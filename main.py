import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Optional

import aiohttp
import yaml
from astrbot import logger
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain, Reply
import astrbot.api.message_components as Comp

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont
except ImportError:
    PILImage = None
    ImageDraw = None
    ImageFont = None

PLUGIN_DATA_DIR = "astrbot_plugin_anime_schedule"

DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DAY_ALIASES: dict[str, int] = {}
for i, name in enumerate(DAY_NAMES, start=1):
    DAY_ALIASES[name] = i
    DAY_ALIASES[f"星期{name[-1]}"] = i
    DAY_ALIASES[f"周{i}"] = i
    DAY_ALIASES[str(i)] = i
DAY_ALIASES.update({"周天": 7, "星期天": 7, "星期日": 7})

DAY_COLORS = [
    (230, 120, 110),
    (245, 170, 120),
    (235, 185, 60),
    (230, 220, 130),
    (150, 200, 140),
    (140, 130, 120),
    (150, 160, 210),
]

HELP_TEXT = (
    "📺 番剧追番表指令一览\n"
    "1. 番剧上传 周X 名称 + 图片：添加番剧（可附图或引用带图消息）\n"
    "2. 番剧图 / 番剧列表：生成每周追番长图\n"
    "3. 今日番剧：查看今天更新的番剧（文字列表 + 单行拼接长图）\n"
    "4. 番剧 周X：查看指定星期的番剧（文字列表 + 单行拼接长图）\n"
    "5. 删除番剧 周X 编号：删除某天指定番剧\n"
    "6. 清空番剧 周X / 清空番剧 全部：清除某天或全部番剧\n"
    "7. 番剧权限+数字：设置上传权限（Bot管理员）\n"
    "8. 番剧设置 每日上限 数字：设置每天上限（Bot管理员）\n"
    "9. 番剧推送 开启/关闭：开关本群每日定时推送\n"
    "10. 番剧推送 时间 8:30：设置每日推送时间（Bot管理员）\n"
    "11. 番剧推送 立即：立即推送今日番剧（测试用）\n"
    "12. 番剧帮助：显示本帮助"
)


def _pick_cjk_font(size: int):
    if ImageFont is None:
        return None
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:\\Windows\\Fonts\\msyh.ttc",
        "C:\\Windows\\Fonts\\simhei.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default()
    except OSError:
        return None


def _parse_day_token(token: str) -> Optional[int]:
    token = (token or "").strip()
    if not token:
        return None
    return DAY_ALIASES.get(token) or DAY_ALIASES.get(token.replace(" ", ""))


def _today_weekday() -> int:
    """返回 1=周一 … 7=周日。"""
    return datetime.now().isoweekday()


@register(
    "anime_schedule",
    "buluge",
    "按周一至周日记录追番列表，支持图片+文字上传与周表长图生成",
    "1.0.0",
)
class AnimeSchedulePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_root = os.path.join("data", "plugin_data", PLUGIN_DATA_DIR)
        bot_config = context.get_config()
        admins = bot_config.get("admins_id", [])
        self.admins = [str(a) for a in admins] if admins else []
        self.upload_mode_default = int(getattr(config, "upload_mode_default", None) or 2)
        self.max_per_day_default = int(getattr(config, "max_per_day_default", None) or 10)
        self.push_hour_default = int(getattr(config, "push_hour_default", None) or 8)
        self.push_minute_default = int(getattr(config, "push_minute_default", None) or 0)
        self._push_task: Optional[asyncio.Task] = None

    async def initialize(self):
        self._push_task = asyncio.create_task(self._push_scheduler_loop())
        logger.info("番剧追番表：定时推送任务已启动")

    async def terminate(self):
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
            self._push_task = None
        logger.info("番剧追番表：定时推送任务已停止")

    def _default_settings(self) -> dict:
        return {
            "upload_mode": self.upload_mode_default,
            "max_per_day": self.max_per_day_default,
            "push_enabled": False,
            "push_hour": self.push_hour_default,
            "push_minute": self.push_minute_default,
            "push_umo": None,
            "last_push_date": None,
        }

    def _group_dir(self, group_id: str) -> str:
        path = os.path.join(self.data_root, str(group_id))
        os.makedirs(path, exist_ok=True)
        return path

    def _schedule_path(self, group_id: str) -> str:
        return os.path.join(self._group_dir(group_id), "schedule.json")

    def _settings_path(self, group_id: str) -> str:
        return os.path.join(self._group_dir(group_id), "settings.yml")

    def _load_settings(self, group_id: str) -> dict:
        path = self._settings_path(group_id)
        defaults = self._default_settings()
        if not os.path.isfile(path):
            return dict(defaults)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for key, value in defaults.items():
                data.setdefault(key, value)
            return data
        except Exception as e:
            logger.warning(f"加载番剧设置失败: {e}")
            return dict(defaults)

    def _iter_group_ids(self) -> list[str]:
        if not os.path.isdir(self.data_root):
            return []
        group_ids = []
        for name in os.listdir(self.data_root):
            path = os.path.join(self.data_root, name)
            if not os.path.isdir(path):
                continue
            if os.path.isfile(os.path.join(path, "schedule.json")) or os.path.isfile(
                os.path.join(path, "settings.yml")
            ):
                group_ids.append(name)
        return group_ids

    def _parse_push_time(self, text: str) -> Optional[tuple[int, int]]:
        text = (text or "").strip().replace("：", ":")
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", text)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour, minute

    def _format_push_time(self, hour: int, minute: int) -> str:
        return f"{hour:02d}:{minute:02d}"

    async def _push_scheduler_loop(self):
        while True:
            try:
                await self._check_and_push_all_groups()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"番剧定时推送检查失败: {e}")
            await asyncio.sleep(30)

    async def _check_and_push_all_groups(self):
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        for group_id in self._iter_group_ids():
            settings = self._load_settings(group_id)
            if not settings.get("push_enabled"):
                continue
            umo = settings.get("push_umo")
            if not umo:
                continue
            hour = int(settings.get("push_hour", self.push_hour_default))
            minute = int(settings.get("push_minute", self.push_minute_default))
            if now.hour != hour or now.minute != minute:
                continue
            if settings.get("last_push_date") == today_str:
                continue
            ok = await self._send_today_push(group_id, umo, header="⏰ 今日番剧定时推送")
            if ok:
                settings["last_push_date"] = today_str
                self._save_settings(group_id, settings)
                logger.info(f"番剧定时推送成功: group={group_id}")

    async def _send_today_push(self, group_id: str, umo: str, header: str = "") -> bool:
        day_idx = _today_weekday()
        schedule = self._load_schedule(group_id)
        entries = schedule.get(str(day_idx), [])
        text = self._format_day_entries_text(day_idx, entries)
        if header:
            text = f"{header}\n{text}"
        try:
            await self.context.send_message(umo, MessageChain().message(text))
            out = self._build_schedule_image(group_id, highlight_day=day_idx, only_day=day_idx)
            if out:
                await self.context.send_message(umo, MessageChain().file_image(out))
            return True
        except Exception as e:
            logger.error(f"番剧推送发送失败 group={group_id}: {e}")
            return False

    def _save_settings(self, group_id: str, settings: dict):
        path = self._settings_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(settings, f, allow_unicode=True)

    def _empty_schedule(self) -> dict:
        return {str(i): [] for i in range(1, 8)}

    def _load_schedule(self, group_id: str) -> dict:
        path = self._schedule_path(group_id)
        if not os.path.isfile(path):
            return self._empty_schedule()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            schedule = self._empty_schedule()
            if isinstance(data, dict):
                for key, entries in data.items():
                    if key in schedule and isinstance(entries, list):
                        schedule[key] = entries
            return schedule
        except Exception as e:
            logger.error(f"读取番剧数据失败: {e}")
            return self._empty_schedule()

    def _save_schedule(self, group_id: str, schedule: dict):
        path = self._schedule_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self.admins

    def _can_upload(self, group_id: str, user_id: str) -> tuple[bool, str]:
        settings = self._load_settings(group_id)
        mode = int(settings.get("upload_mode", self.upload_mode_default))
        if mode == 0:
            return False, "投稿系统未开启，请联系 Bot 管理员发送「番剧权限」设置"
        if mode == 1 and not self.is_admin(user_id):
            return False, "当前为「仅管理员可上传」，请联系 Bot 管理员"
        return True, ""

    def _can_manage(self, user_id: str) -> bool:
        return self.is_admin(user_id)

    async def _save_bytes_as_image(self, group_id: str, data: bytes) -> str:
        filename = f"anime_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.jpg"
        file_path = os.path.join(self._group_dir(group_id), filename)
        with open(file_path, "wb") as f:
            f.write(data)
        return filename

    async def download_image(
        self,
        event: AstrMessageEvent,
        group_id: str,
        file_id: Optional[str] = None,
        image_comp: Optional[Image] = None,
    ) -> Optional[str]:
        try:
            image_obj = image_comp
            if image_obj is None:
                for part in event.message_obj.message or []:
                    if isinstance(part, Image):
                        image_obj = part
                        break
            if image_obj:
                local_path = await image_obj.convert_to_file_path()
                if local_path and os.path.isfile(local_path):
                    with open(local_path, "rb") as f:
                        return await self._save_bytes_as_image(group_id, f.read())

            if file_id:
                try:
                    result = await event.bot.api.call_action("get_image", file_id=file_id)
                except Exception as e:
                    logger.warning(f"get_image 失败: {e}")
                    result = {}
                api_path = result.get("file") if isinstance(result, dict) else None
                if api_path and os.path.isfile(api_path):
                    with open(api_path, "rb") as f:
                        return await self._save_bytes_as_image(group_id, f.read())
                url = result.get("url") if isinstance(result, dict) else None
                if not url and image_obj:
                    url = getattr(image_obj, "url", None)
                if url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=15) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                return await self._save_bytes_as_image(group_id, data)
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
        return None

    async def _extract_image_from_reply(self, event: AstrMessageEvent, group_id: str) -> tuple[Optional[str], Optional[str]]:
        reply_comp = next((m for m in (event.message_obj.message or []) if isinstance(m, Reply)), None)
        if not reply_comp:
            return None, None
        file_id = None
        plain = ""
        try:
            reply_id = int(reply_comp.id) if str(reply_comp.id).isdigit() else reply_comp.id
            reply_msg = await event.bot.api.call_action("get_msg", message_id=reply_id)
            chain = reply_msg.get("message") if isinstance(reply_msg, dict) else None
            if isinstance(chain, list):
                for part in chain:
                    if isinstance(part, dict):
                        if part.get("type") == "image":
                            file_id = part.get("data", {}).get("file")
                        elif part.get("type") == "text":
                            plain += part.get("data", {}).get("text", "")
            elif isinstance(chain, str):
                plain = re.sub(r"\[CQ:[^\]]+\]", "", chain).strip()
                m = re.search(r"\[CQ:image,[^\]]*file=([^,\]]+)", chain)
                if m:
                    file_id = m.group(1)
        except Exception as e:
            logger.error(f"解析引用消息失败: {e}")
        image_name = await self.download_image(event, group_id, file_id=file_id) if file_id else None
        return image_name, plain.strip() or None

    def _remove_image_file(self, group_id: str, image_name: Optional[str]):
        if not image_name:
            return
        path = os.path.join(self._group_dir(group_id), image_name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"删除图片失败 {path}: {e}")

    def _build_schedule_image(
        self,
        group_id: str,
        highlight_day: Optional[int] = None,
        only_day: Optional[int] = None,
    ) -> Optional[str]:
        if PILImage is None or ImageDraw is None:
            return None

        schedule = self._load_schedule(group_id)
        label_w = 88
        poster_h = 220
        poster_gap = 10
        row_gap = 8
        margin = 12
        canvas_w = 1080
        poster_area_w = canvas_w - margin * 2 - label_w - poster_gap
        day_range = [only_day] if only_day is not None else list(range(1, 8))
        max_posters = max(1, max((len(schedule.get(str(i), [])) for i in day_range), default=1))
        poster_w = max(120, int((poster_area_w - poster_gap * (max_posters - 1)) / max_posters))
        row_h = poster_h + 16
        num_rows = len(day_range)
        canvas_h = margin * 2 + row_h * num_rows + row_gap * max(0, num_rows - 1)

        img = PILImage.new("RGB", (canvas_w, canvas_h), (18, 18, 20))
        draw = ImageDraw.Draw(img)
        font_day = _pick_cjk_font(28)
        font_title = _pick_cjk_font(16)
        if font_day is None:
            return None

        y = margin
        for day_idx in day_range:
            day_key = str(day_idx)
            entries = schedule.get(day_key, [])
            color = DAY_COLORS[day_idx - 1]
            x_label = margin
            draw.rectangle(
                [x_label, y, x_label + label_w, y + row_h],
                fill=color,
            )
            day_text = DAY_NAMES[day_idx - 1]
            if highlight_day == day_idx:
                draw.rectangle(
                    [x_label - 2, y - 2, x_label + label_w + poster_area_w + poster_gap + 2, y + row_h + 2],
                    outline=(255, 220, 80),
                    width=3,
                )
            tw = draw.textlength(day_text, font=font_day) if hasattr(draw, "textlength") else 56
            draw.text(
                (x_label + (label_w - tw) / 2, y + (row_h - 28) / 2),
                day_text,
                fill=(30, 30, 30),
                font=font_day,
            )

            x_poster = x_label + label_w + poster_gap
            if not entries:
                draw.text((x_poster + 12, y + row_h // 2 - 10), "（暂无番剧）", fill=(120, 120, 130), font=font_title)
            else:
                for entry in entries:
                    image_name = entry.get("image")
                    title = (entry.get("title") or "").strip()
                    full_path = os.path.join(self._group_dir(group_id), image_name) if image_name else ""
                    if full_path and os.path.isfile(full_path):
                        try:
                            with PILImage.open(full_path) as src:
                                src = src.convert("RGB")
                                ratio = min(poster_w / max(1, src.width), poster_h / max(1, src.height))
                                nw = max(1, int(src.width * ratio))
                                nh = max(1, int(src.height * ratio))
                                resized = src.resize((nw, nh))
                                px = x_poster + (poster_w - nw) // 2
                                py = y + 8 + (poster_h - nh) // 2
                                img.paste(resized, (px, py))
                        except Exception:
                            draw.rectangle([x_poster, y + 8, x_poster + poster_w, y + 8 + poster_h], fill=(60, 60, 68))
                            draw.text((x_poster + 8, y + poster_h // 2), "加载失败", fill=(200, 200, 200), font=font_title)
                    else:
                        draw.rectangle([x_poster, y + 8, x_poster + poster_w, y + 8 + poster_h], fill=(50, 50, 58))
                        draw.text((x_poster + 8, y + poster_h // 2), "无封面", fill=(180, 180, 190), font=font_title)

                    if title and font_title:
                        title_show = title if len(title) <= 10 else title[:9] + "…"
                        tw2 = draw.textlength(title_show, font=font_title) if hasattr(draw, "textlength") else 80
                        draw.text(
                            (x_poster + max(0, (poster_w - tw2) / 2), y + 8 + poster_h + 2),
                            title_show,
                            fill=(220, 220, 230),
                            font=font_title,
                        )
                    x_poster += poster_w + poster_gap
            y += row_h + row_gap

        prefix = f"day_{only_day}" if only_day is not None else "schedule"
        out_path = os.path.join(self._group_dir(group_id), f"{prefix}_{int(time.time())}.png")
        img.save(out_path, format="PNG")
        return out_path

    def _format_day_entries_text(self, day_idx: int, entries: list) -> str:
        if not entries:
            return f"📺 {DAY_NAMES[day_idx - 1]}：暂无番剧"
        lines = [f"📺 {DAY_NAMES[day_idx - 1]} 共 {len(entries)} 部："]
        for i, entry in enumerate(entries, 1):
            title = entry.get("title") or f"番剧#{i}"
            lines.append(f"  {i}. {title}")
        return "\n".join(lines)

    async def _yield_day_row(self, event: AstrMessageEvent, group_id: str, day_idx: int):
        schedule = self._load_schedule(group_id)
        entries = schedule.get(str(day_idx), [])
        yield event.plain_result(self._format_day_entries_text(day_idx, entries))
        if PILImage is None:
            if entries:
                yield event.plain_result("服务器未安装 Pillow，无法生成拼接图。请安装依赖：pip install Pillow")
            return
        out = self._build_schedule_image(group_id, highlight_day=day_idx, only_day=day_idx)
        if out:
            yield event.image_result(out)

    @filter.command("番剧帮助", alias={"/番剧帮助", "番剧菜单"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_help(self, event: AstrMessageEvent):
        event.call_llm = True
        yield event.plain_result(HELP_TEXT)

    @filter.command("番剧图", alias={"/番剧图", "番剧列表", "/番剧列表"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_schedule_image(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        if PILImage is None:
            yield event.plain_result("服务器未安装 Pillow，无法生成周表长图。请安装依赖：pip install Pillow")
            return
        out = self._build_schedule_image(group_id)
        if not out:
            yield event.plain_result("暂无番剧数据，或图片生成失败。请先使用「番剧上传」添加番剧。")
            return
        yield event.image_result(out)

    @filter.command("今日番剧", alias={"/今日番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_today(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        day_idx = _today_weekday()
        async for res in self._yield_day_row(event, group_id, day_idx):
            yield res

    @filter.command("番剧")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_day_query(self, event: AstrMessageEvent, day_token: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        day_idx = _parse_day_token(day_token)
        if not day_idx:
            yield event.plain_result("用法：番剧 周X（如 番剧 周一）")
            return
        async for res in self._yield_day_row(event, group_id, day_idx):
            yield res

    @filter.command("番剧上传", alias={"/番剧上传"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_upload(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        msg = event.message_str.strip()

        can, deny = self._can_upload(group_id, user_id)
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        m = re.match(r"^/?番剧上传\s+(\S+)\s+(.+)$", msg)
        if not m:
            yield event.plain_result(
                "用法：番剧上传 周X 番剧名称 + 图片\n"
                "示例：番剧上传 周一 葬送的芙莉莲（附图或引用带图消息）"
            )
            return

        day_idx = _parse_day_token(m.group(1))
        title = m.group(2).strip()
        if not day_idx:
            yield event.plain_result(f"无法识别星期：{m.group(1)}，请使用 周一~周日")
            return
        if not title:
            yield event.plain_result("请填写番剧名称")
            return

        settings = self._load_settings(group_id)
        max_per_day = max(1, min(20, int(settings.get("max_per_day", self.max_per_day_default))))
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        if len(schedule.get(day_key, [])) >= max_per_day:
            yield event.plain_result(f"❌ {DAY_NAMES[day_idx - 1]} 已达上限（{max_per_day} 部），请先删除或清空")
            return

        image_comp = next((p for p in (event.message_obj.message or []) if isinstance(p, Image)), None)
        reply_image, reply_text = await self._extract_image_from_reply(event, group_id)
        file_id = image_comp.file if image_comp else None
        image_name = await self.download_image(event, group_id, file_id=file_id, image_comp=image_comp)
        if not image_name and reply_image:
            image_name = reply_image
        if not image_name:
            yield event.plain_result("请附带图片，或引用一条带图的消息")
            return
        if not title and reply_text:
            title = reply_text

        entry = {
            "id": str(uuid.uuid4()),
            "title": title,
            "image": image_name,
            "added_by": user_id,
            "added_at": int(time.time()),
        }
        schedule.setdefault(day_key, []).append(entry)
        self._save_schedule(group_id, schedule)
        idx = len(schedule[day_key])
        yield event.chain_result([
            Comp.Reply(id=str(event.message_obj.message_id)),
            Comp.Plain(f"✅ 已添加到 {DAY_NAMES[day_idx - 1]}：{title}（#{idx}）"),
        ])

    @filter.command("删除番剧", alias={"/删除番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_delete_one(self, event: AstrMessageEvent, day_token: str = "", index: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        if not self._can_manage(user_id):
            yield event.plain_result("权限不足，仅 Bot 管理员可删除番剧")
            return

        day_idx = _parse_day_token(day_token)
        if not day_idx or not str(index).strip().isdigit():
            yield event.plain_result("用法：删除番剧 周X 编号（如 删除番剧 周一 1）")
            return

        idx = int(index)
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        if idx < 1 or idx > len(entries):
            yield event.plain_result(f"编号超出范围，{DAY_NAMES[day_idx - 1]} 当前共 {len(entries)} 部")
            return

        removed = entries.pop(idx - 1)
        self._remove_image_file(group_id, removed.get("image"))
        schedule[day_key] = entries
        self._save_schedule(group_id, schedule)
        yield event.plain_result(f"✅ 已删除 {DAY_NAMES[day_idx - 1]} #{idx}：{removed.get('title', '')}")

    @filter.command("清空番剧", alias={"/清空番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_clear(self, event: AstrMessageEvent, target: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        if not self._can_manage(user_id):
            yield event.plain_result("权限不足，仅 Bot 管理员可清空番剧")
            return

        target = (target or "").strip()
        schedule = self._load_schedule(group_id)

        if target in ("", "全部", "all", "ALL"):
            count = sum(len(v) for v in schedule.values())
            for day_key, entries in schedule.items():
                for entry in entries:
                    self._remove_image_file(group_id, entry.get("image"))
                schedule[day_key] = []
            self._save_schedule(group_id, schedule)
            yield event.plain_result(f"✅ 已清空全部番剧，共 {count} 部")
            return

        day_idx = _parse_day_token(target)
        if not day_idx:
            yield event.plain_result("用法：清空番剧 周X  或  清空番剧 全部")
            return

        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        count = len(entries)
        for entry in entries:
            self._remove_image_file(group_id, entry.get("image"))
        schedule[day_key] = []
        self._save_schedule(group_id, schedule)
        yield event.plain_result(f"✅ 已清空 {DAY_NAMES[day_idx - 1]}，共 {count} 部")

    @filter.command("番剧权限")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_upload_mode(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        if not self._can_manage(user_id):
            yield event.plain_result("权限不足，仅 Bot 管理员可设置")
            return

        msg = event.message_str.strip()
        m = re.search(r"(\d+)", msg)
        settings = self._load_settings(group_id)
        current = int(settings.get("upload_mode", self.upload_mode_default))
        if not m:
            yield event.plain_result(
                f"当前上传权限：{current}\n"
                "0：关闭上传\n1：仅 Bot 管理员可上传\n2：全体成员可上传\n"
                "设置示例：番剧权限2"
            )
            return

        mode = int(m.group(1))
        if mode not in (0, 1, 2):
            yield event.plain_result("模式只能是 0、1 或 2")
            return
        settings["upload_mode"] = mode
        self._save_settings(group_id, settings)
        labels = {0: "关闭上传", 1: "仅管理员可上传", 2: "全体成员可上传"}
        yield event.plain_result(f"✅ 番剧上传权限已设为：{labels[mode]}")

    @filter.command("番剧设置")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_settings(self, event: AstrMessageEvent, feature: str = "", value: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        if not self._can_manage(user_id):
            yield event.plain_result("权限不足，仅 Bot 管理员可设置")
            return

        settings = self._load_settings(group_id)
        feature = (feature or "").strip()
        if not feature:
            push_enabled = bool(settings.get("push_enabled"))
            push_hour = int(settings.get("push_hour", self.push_hour_default))
            push_minute = int(settings.get("push_minute", self.push_minute_default))
            yield event.plain_result(
                f"番剧设置\n"
                f"每日上限：{settings.get('max_per_day', self.max_per_day_default)}\n"
                f"上传权限：{settings.get('upload_mode', self.upload_mode_default)}\n"
                f"定时推送：{'开启' if push_enabled else '关闭'}（{self._format_push_time(push_hour, push_minute)}）\n"
                "设置示例：番剧设置 每日上限 10"
            )
            return

        if feature in ("每日上限", "上限"):
            if not str(value).strip().isdigit():
                yield event.plain_result("用法：番剧设置 每日上限 10（1~20）")
                return
            n = max(1, min(20, int(value)))
            settings["max_per_day"] = n
            self._save_settings(group_id, settings)
            yield event.plain_result(f"✅ 每天最多可添加 {n} 部番剧")
            return

        yield event.plain_result("未知设置项，目前支持：每日上限")

    @filter.command("番剧推送", alias={"/番剧推送"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_push(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        msg = event.message_str.strip()
        settings = self._load_settings(group_id)

        if msg in ("番剧推送", "/番剧推送"):
            enabled = bool(settings.get("push_enabled"))
            hour = int(settings.get("push_hour", self.push_hour_default))
            minute = int(settings.get("push_minute", self.push_minute_default))
            status = "已开启" if enabled else "已关闭"
            yield event.plain_result(
                f"📺 本群定时推送：{status}\n"
                f"推送时间：每天 {self._format_push_time(hour, minute)}\n"
                "指令：\n"
                "  番剧推送 开启\n"
                "  番剧推送 关闭\n"
                "  番剧推送 时间 8:30\n"
                "  番剧推送 立即"
            )
            return

        if msg in ("番剧推送 开启", "/番剧推送 开启"):
            if not self._can_manage(user_id):
                yield event.plain_result("权限不足，仅 Bot 管理员可设置定时推送")
                return
            settings["push_enabled"] = True
            settings["push_umo"] = event.unified_msg_origin
            self._save_settings(group_id, settings)
            hour = int(settings.get("push_hour", self.push_hour_default))
            minute = int(settings.get("push_minute", self.push_minute_default))
            yield event.plain_result(
                f"✅ 已开启本群定时推送，每天 {self._format_push_time(hour, minute)} 自动发送今日番剧"
            )
            return

        if msg in ("番剧推送 关闭", "/番剧推送 关闭"):
            if not self._can_manage(user_id):
                yield event.plain_result("权限不足，仅 Bot 管理员可设置定时推送")
                return
            settings["push_enabled"] = False
            self._save_settings(group_id, settings)
            yield event.plain_result("✅ 已关闭本群定时推送")
            return

        if msg.startswith("番剧推送 时间") or msg.startswith("/番剧推送 时间"):
            if not self._can_manage(user_id):
                yield event.plain_result("权限不足，仅 Bot 管理员可设置定时推送")
                return
            time_text = re.sub(r"^/?番剧推送\s+时间\s*", "", msg).strip()
            parsed = self._parse_push_time(time_text)
            if not parsed:
                yield event.plain_result("用法：番剧推送 时间 8:30（24 小时制，范围 00:00~23:59）")
                return
            hour, minute = parsed
            settings["push_hour"] = hour
            settings["push_minute"] = minute
            settings["last_push_date"] = None
            self._save_settings(group_id, settings)
            yield event.plain_result(f"✅ 定时推送时间已设为每天 {self._format_push_time(hour, minute)}")
            return

        if msg in ("番剧推送 立即", "/番剧推送 立即"):
            if not self._can_manage(user_id):
                yield event.plain_result("权限不足，仅 Bot 管理员可测试推送")
                return
            umo = settings.get("push_umo") or event.unified_msg_origin
            ok = await self._send_today_push(group_id, umo, header="📺 今日番剧（手动测试推送）")
            if ok:
                yield event.plain_result("✅ 测试推送已发送")
            else:
                yield event.plain_result("❌ 推送失败，请检查 Bot 是否有主动发消息权限")
            return

        yield event.plain_result("未知子命令，发送「番剧推送」查看帮助")
