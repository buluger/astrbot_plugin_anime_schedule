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
    from .entry_progress import (
        DEFAULT_MAX_EP,
        _is_finished,
        _is_paused,
        _is_watched_today,
        _mark_watched_one,
        _normalize_entry,
        _pause_one,
        _progress_label,
        _resume_one,
        _set_watched_one,
        _should_watch_ep,
        _today_date_str,
        _undo_watched_one,
    )
    from .backlog_progress import (
        add_today,
        apply_emoji,
        format_full_list,
        format_today_push,
        normalize_backlog_entry,
        parse_upload_block,
        pick_suggestions,
        poster_caption,
        set_today_count,
        set_watched_total,
        sink_finished,
        undo_emoji,
        undo_today,
    )
except ImportError:
    from entry_progress import (
        DEFAULT_MAX_EP,
        _is_finished,
        _is_paused,
        _is_watched_today,
        _mark_watched_one,
        _normalize_entry,
        _pause_one,
        _progress_label,
        _resume_one,
        _set_watched_one,
        _should_watch_ep,
        _today_date_str,
        _undo_watched_one,
    )
    from backlog_progress import (
        add_today,
        apply_emoji,
        format_full_list,
        format_today_push,
        normalize_backlog_entry,
        parse_upload_block,
        pick_suggestions,
        poster_caption,
        set_today_count,
        set_watched_total,
        sink_finished,
        undo_emoji,
        undo_today,
    )

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

# 1=仅Bot管理员 2=仅群主 3=群主+管理 4=全员
PERM_LEVELS = {
    1: "仅 Bot 管理员",
    2: "仅群主",
    3: "群主+管理员",
    4: "全员",
}

BANGUMI_API = "https://api.bgm.tv"
BANGUMI_UA = (
    "astrbot_plugin_anime_schedule/1.9 "
    "(https://github.com/buluger/astrbot_plugin_anime_schedule)"
)

# 表情回应：标记「看过」的系统表情 / emoji（NapCat likes.emoji_id）
DONE_REACTION_IDS = {
    "76",   # 强
    "124",  # OK
    "177",  # 鼓掌
    "201",  # 赞
    "36",   # 乖
    "66",   # 爱心
    "79",   # 握手
    "178",  # 抱拳
    "👍",
    "✅",
    "✔️",
    "👌",
    "👏",
    "💯",
}
# 数字表情 → 列表编号（1-based）
NUM_REACTION_IDS = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
    "1️⃣": 1, "2️⃣": 2, "3️⃣": 3, "4️⃣": 4, "5️⃣": 5,
    "6️⃣": 6, "7️⃣": 7, "8️⃣": 8, "9️⃣": 9, "🔟": 10,
}

HELP_TEXT = (
    "📺 番剧追番表指令一览\n"
    "【查看】今日番剧 / 番剧 周X / 番剧图\n"
    "【新番】番剧上传 周X 名称 [总集数] + 图片\n"
    "【进度】已看 / 已看全部 / 撤回 / 番剧已看 周X [编号] N\n"
    "【集数】番剧上限 周X [编号] [N]：同步 Bangumi 或手动设置\n"
    "【停播】番剧暂停/恢复 周X 编号（停播时「已看」会跳过）\n"
    "【管理】删除/移动/交换/清空番剧\n"
    "【补番】补番上传 / 补番 / 补了 / 补番已看 / 补番撤回 / 补番删除\n"
    "【设置】番剧权限1~4 / 番剧设置 每日上限|观看名额 / 番剧推送\n"
    "【表情】新番：数字=+1，赞/OK/强=全部；补番：1~3 集\n"
    "详情可看 README，或对具体命令少写参数查看用法。"
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


def _pick_bangumi_eps(detail: dict) -> Optional[int]:
    """从 Bangumi 条目详情解析总集数。"""
    for key in ("total_episodes", "eps", "eps_count"):
        val = detail.get(key)
        if isinstance(val, int) and val > 0:
            return min(999, val)
        if isinstance(val, str) and val.isdigit() and int(val) > 0:
            return min(999, int(val))
    for item in detail.get("infobox") or []:
        if not isinstance(item, dict):
            continue
        if item.get("key") in ("话数", "集数"):
            val = item.get("value")
            if isinstance(val, (int, float)) and int(val) > 0:
                return min(999, int(val))
            if isinstance(val, str):
                m = re.search(r"(\d+)", val)
                if m and int(m.group(1)) > 0:
                    return min(999, int(m.group(1)))
    return None


def _bangumi_name_score(title: str, item: dict) -> int:
    """名称匹配分，越高越好。"""
    title_n = re.sub(r"\s+", "", title).lower()
    name_cn = re.sub(r"\s+", "", str(item.get("name_cn") or "")).lower()
    name = re.sub(r"\s+", "", str(item.get("name") or "")).lower()
    if not title_n:
        return 0
    if title_n == name_cn or title_n == name:
        return 100
    if title_n and (title_n in name_cn or title_n in name):
        return 80
    if name_cn and name_cn in title_n:
        return 70
    if name and name in title_n:
        return 60
    return 0


async def fetch_bangumi_max_ep(title: str) -> tuple[Optional[int], Optional[int], str]:
    """
    按番名查询 Bangumi，返回 (max_ep, bangumi_id, message)。
    失败时 max_ep 为 None。
    """
    title = (title or "").strip()
    if not title:
        return None, None, "番名为空"
    headers = {
        "User-Agent": BANGUMI_UA,
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            subject = None
            # 优先新版搜索
            try:
                async with session.post(
                    f"{BANGUMI_API}/v0/search/subjects",
                    json={
                        "keyword": title,
                        "filter": {"type": [2]},
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("data") if isinstance(data, dict) else None
                        if isinstance(items, list) and items:
                            ranked = sorted(
                                items,
                                key=lambda x: _bangumi_name_score(title, x),
                                reverse=True,
                            )
                            if _bangumi_name_score(title, ranked[0]) > 0 or len(ranked) == 1:
                                subject = ranked[0]
            except Exception as e:
                logger.warning(f"Bangumi v0 搜索失败: {e}")

            # 回退旧版搜索
            if subject is None:
                from urllib.parse import quote

                url = (
                    f"{BANGUMI_API}/search/subject/{quote(title)}"
                    f"?type=2&responseGroup=small&max_results=10"
                )
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None, None, f"Bangumi 搜索失败（HTTP {resp.status}）"
                    data = await resp.json(content_type=None)
                    items = []
                    if isinstance(data, dict):
                        items = data.get("list") or data.get("data") or []
                    elif isinstance(data, list):
                        items = data
                    if not items:
                        return None, None, f"Bangumi 未找到「{title}」"
                    ranked = sorted(
                        items,
                        key=lambda x: _bangumi_name_score(title, x),
                        reverse=True,
                    )
                    subject = ranked[0]

            sid = subject.get("id")
            if sid is None:
                return None, None, f"Bangumi 未找到「{title}」"
            bgm_name = subject.get("name_cn") or subject.get("name") or title

            # 详情取更准确的集数
            detail = subject
            try:
                async with session.get(f"{BANGUMI_API}/v0/subjects/{int(sid)}") as resp:
                    if resp.status == 200:
                        detail = await resp.json()
            except Exception as e:
                logger.warning(f"Bangumi 详情失败，使用搜索结果: {e}")

            eps = _pick_bangumi_eps(detail)
            matched = detail.get("name_cn") or detail.get("name") or bgm_name
            if not eps:
                return None, int(sid), f"已匹配「{matched}」，但 Bangumi 暂无总集数"
            return eps, int(sid), f"已匹配 Bangumi「{matched}」（ID {sid}）→ {eps} 集"
    except asyncio.TimeoutError:
        return None, None, "查询 Bangumi 超时"
    except Exception as e:
        logger.error(f"查询 Bangumi 失败: {e}")
        return None, None, f"查询 Bangumi 失败：{e}"


@register(
    "anime_schedule",
    "buluge",
    "按周一至周日记录追番列表，支持补番队列、进度追踪与周表长图",
    "1.9.0",
)
class AnimeSchedulePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_root = os.path.join("data", "plugin_data", PLUGIN_DATA_DIR)
        bot_config = context.get_config()
        admins = bot_config.get("admins_id", [])
        self.admins = [str(a) for a in admins] if admins else []
        # 兼容旧配置 upload_mode_default；默认 3=群主+管理员
        if getattr(config, "perm_mode_default", None) is not None:
            self.perm_mode_default = self._clamp_perm_mode(config.perm_mode_default, 3)
        elif getattr(config, "upload_mode_default", None) is not None:
            self.perm_mode_default = self._migrate_legacy_upload_mode(config.upload_mode_default, 3)
        else:
            self.perm_mode_default = 3
        self.max_per_day_default = int(getattr(config, "max_per_day_default", None) or 10)
        self.watch_quota_default = self._clamp_watch_quota(
            getattr(config, "watch_quota_default", None), 3
        )
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

    @staticmethod
    def _clamp_perm_mode(value, fallback: int = 3) -> int:
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return fallback
        return mode if mode in PERM_LEVELS else fallback

    @staticmethod
    def _clamp_watch_quota(value, fallback: int = 3) -> int:
        """观看名额只用于补番补位，范围 1~10。"""
        try:
            quota = int(value)
        except (TypeError, ValueError):
            quota = fallback
        if quota < 1 or quota > 10:
            try:
                quota = int(fallback)
            except (TypeError, ValueError):
                quota = 3
        return max(1, min(10, quota))

    @staticmethod
    def _migrate_legacy_upload_mode(value, fallback: int = 3) -> int:
        """旧 upload_mode：0关闭 / 1仅Bot / 2全员 → 新 perm_mode。"""
        try:
            old = int(value)
        except (TypeError, ValueError):
            return fallback
        return {0: 1, 1: 1, 2: 4}.get(old, fallback)

    def _get_perm_mode(self, group_id: str) -> int:
        settings = self._load_settings(group_id)
        return self._clamp_perm_mode(settings.get("perm_mode"), self.perm_mode_default)

    def _default_settings(self) -> dict:
        return {
            "perm_mode": self.perm_mode_default,
            "max_per_day": self.max_per_day_default,
            "watch_quota": self.watch_quota_default,
            "push_enabled": False,
            "push_hour": self.push_hour_default,
            "push_minute": self.push_minute_default,
            "push_umo": None,
            "last_push_date": None,
            "last_scheduled_push_date": None,
            "last_push_message_ids": [],
            "last_push_day": None,
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
            if "perm_mode" not in data and "upload_mode" in data:
                data["perm_mode"] = self._migrate_legacy_upload_mode(
                    data.get("upload_mode"), self.perm_mode_default
                )
            for key, value in defaults.items():
                data.setdefault(key, value)
            data["perm_mode"] = self._clamp_perm_mode(data.get("perm_mode"), self.perm_mode_default)
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
            if (
                os.path.isfile(os.path.join(path, "schedule.json"))
                or os.path.isfile(os.path.join(path, "settings.yml"))
                or os.path.isfile(os.path.join(path, "backlog.json"))
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

    def _should_run_scheduled_push(self, settings: dict, now: datetime) -> bool:
        """
        判断本群此刻是否应发定时推送。

        用 last_scheduled_push_date 记「今天定时是否已发」，避免「立即」测试挡住晚上的定时。
        到点后 10 分钟内都会补发，避免 30 秒轮询刚好错过整分。
        """
        if not settings.get("push_enabled"):
            return False
        if not settings.get("push_umo"):
            return False
        today_str = now.strftime("%Y-%m-%d")
        if settings.get("last_scheduled_push_date") == today_str:
            return False
        hour = int(settings.get("push_hour", self.push_hour_default))
        minute = int(settings.get("push_minute", self.push_minute_default))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (now - scheduled).total_seconds()
        return 0 <= delta < 600

    async def _check_and_push_all_groups(self):
        now = datetime.now()
        for group_id in self._iter_group_ids():
            settings = self._load_settings(group_id)
            if not self._should_run_scheduled_push(settings, now):
                continue
            umo = settings.get("push_umo")
            ok = await self._send_today_push(
                group_id, umo, header="⏰ 今日番剧定时推送", mark_scheduled=True
            )
            if ok:
                logger.info(f"番剧定时推送成功: group={group_id}")
            else:
                logger.warning(f"番剧定时推送失败，将在宽限窗口内重试: group={group_id}")

    async def _send_today_push(
        self,
        group_id: str,
        umo: str,
        header: str = "",
        bot=None,
        mark_scheduled: bool = False,
    ) -> bool:
        day_idx = _today_weekday()
        schedule = self._load_schedule(group_id)
        entries = schedule.get(str(day_idx), [])
        text = self._format_day_entries_text(day_idx, entries, with_tip=True)
        if header:
            text = f"{header}\n{text}"
        backlog_text = self._format_today_backlog(group_id, day_idx)
        try:
            msg_ids = []
            text_id = await self._send_group_payload(umo, group_id, text=text, bot=bot)
            if text_id:
                msg_ids.append(str(text_id))
            out = self._build_schedule_image(group_id, highlight_day=day_idx, only_day=day_idx)
            if out:
                img_id = await self._send_group_payload(umo, group_id, image_path=out, bot=bot)
                if img_id:
                    msg_ids.append(str(img_id))
            backlog_ids = []
            if backlog_text:
                backlog_id = await self._send_group_payload(umo, group_id, text=backlog_text, bot=bot)
                if backlog_id:
                    backlog_ids.append(str(backlog_id))
            settings = self._load_settings(group_id)
            settings["last_push_message_ids"] = msg_ids
            settings["last_backlog_message_ids"] = backlog_ids
            settings["last_push_day"] = day_idx
            settings["last_push_date"] = _today_date_str()
            if mark_scheduled:
                settings["last_scheduled_push_date"] = _today_date_str()
            self._save_settings(group_id, settings)
            return True
        except Exception as e:
            logger.error(f"番剧推送发送失败 group={group_id}: {e}")
            return False

    async def _send_group_payload(
        self,
        umo: str,
        group_id: str,
        text: Optional[str] = None,
        image_path: Optional[str] = None,
        bot=None,
    ) -> Optional[str]:
        """发送群消息并尽量返回 message_id（用于表情回应）。"""
        client = bot
        if client is None:
            client = self._get_bot_client(umo)
        if client is not None and group_id:
            try:
                if text is not None:
                    ret = await client.api.call_action(
                        "send_group_msg",
                        group_id=int(group_id),
                        message=text,
                    )
                elif image_path:
                    ret = await client.api.call_action(
                        "send_group_msg",
                        group_id=int(group_id),
                        message=[{"type": "image", "data": {"file": f"file://{os.path.abspath(image_path)}"}}],
                    )
                else:
                    return None
                if isinstance(ret, dict) and ret.get("message_id") is not None:
                    return str(ret.get("message_id"))
                if isinstance(ret, (int, str)):
                    return str(ret)
            except Exception as e:
                logger.warning(f"协议端直发失败，回退 context.send_message: {e}")
        if text is not None:
            await self.context.send_message(umo, MessageChain().message(text))
        elif image_path:
            await self.context.send_message(umo, MessageChain().file_image(image_path))
        return None

    def _get_bot_client(self, umo: str = ""):
        try:
            platform_id = (umo or "").split(":")[0]
            if platform_id:
                platform = self.context.get_platform_inst(platform_id)
                if platform and hasattr(platform, "get_client"):
                    return platform.get_client()
        except Exception as e:
            logger.debug(f"获取平台 client 失败: {e}")
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms or []:
                name = getattr(getattr(platform, "meta", None), "name", "") or getattr(
                    platform, "platform_name", ""
                )
                if name == "aiocqhttp" and hasattr(platform, "get_client"):
                    return platform.get_client()
        except Exception:
            pass
        return None

    async def _resolve_member_role(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        role = self._get_group_role(event) if event else "member"
        if role != "member":
            return role
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None) if event else None
        if isinstance(raw, dict) and raw.get("post_type") == "notice":
            bot = getattr(event, "bot", None) or self._get_bot_client(getattr(event, "unified_msg_origin", ""))
            if bot:
                try:
                    info = await bot.api.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        no_cache=True,
                    )
                    r = (info or {}).get("role")
                    if r in ("owner", "admin", "member"):
                        return r
                except Exception as e:
                    logger.debug(f"查询群成员身份失败: {e}")
        return role

    async def _check_perm_async(
        self, event: AstrMessageEvent, group_id: str, action: str = "操作"
    ) -> tuple[bool, str]:
        user_id = str(event.get_sender_id())
        if self.is_bot_admin(user_id):
            return True, ""
        mode = self._get_perm_mode(group_id)
        label = PERM_LEVELS.get(mode, str(mode))
        deny = f"权限不足，当前为「{label}」，无法{action}"
        if mode == 4:
            return True, ""
        role = await self._resolve_member_role(event, group_id, user_id)
        if mode == 3 and role in ("owner", "admin"):
            return True, ""
        if mode == 2 and role == "owner":
            return True, ""
        return False, deny

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
                        schedule[key] = [_normalize_entry(e) for e in entries if isinstance(e, dict)]
            return schedule
        except Exception as e:
            logger.error(f"读取番剧数据失败: {e}")
            return self._empty_schedule()

    def _save_schedule(self, group_id: str, schedule: dict):
        path = self._schedule_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)

    def _backlog_path(self, group_id: str) -> str:
        return os.path.join(self._group_dir(group_id), "backlog.json")

    def _load_backlog(self, group_id: str) -> list:
        """读取补番队列；已完结的条目排在末尾。"""
        path = self._backlog_path(group_id)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            entries = [normalize_backlog_entry(e) for e in data if isinstance(e, dict)]
            return sink_finished(entries)
        except Exception as e:
            logger.error(f"读取补番数据失败: {e}")
            return []

    def _save_backlog(self, group_id: str, entries: list):
        path = self._backlog_path(group_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

    def _airing_slot_count(self, group_id: str, day_idx: Optional[int] = None) -> int:
        """今天仍占用观看名额的新番数量。停播的不计入。"""
        day_idx = day_idx or _today_weekday()
        entries = self._load_schedule(group_id).get(str(day_idx), [])
        return sum(1 for entry in entries if not _is_paused(entry))

    def _watch_quota(self, group_id: str) -> int:
        """本群观看名额。只决定今天要不要用补番补位。"""
        settings = self._load_settings(group_id)
        return self._clamp_watch_quota(settings.get("watch_quota"), self.watch_quota_default)

    def _today_fill_entries(self, group_id: str, day_idx: int) -> list:
        """当天未看满时，取出用来补位的补番。"""
        picks = pick_suggestions(
            self._load_backlog(group_id),
            self._airing_slot_count(group_id, day_idx),
            self._watch_quota(group_id),
        )
        return [entry for _index, entry, _suggested in picks]

    def _format_today_backlog(self, group_id: str, day_idx: int) -> Optional[str]:
        """当天有空位时生成补番文字，已满则不追加。"""
        return format_today_push(
            self._load_backlog(group_id),
            self._airing_slot_count(group_id, day_idx),
            quota=self._watch_quota(group_id),
        )

    def _backlog_index_error(self, entries: list, index: int) -> Optional[str]:
        if index < 1 or index > len(entries):
            return f"编号超出范围，当前共 {len(entries)} 部补番"
        return None

    def is_bot_admin(self, user_id: str) -> bool:
        return str(user_id) in self.admins

    def _get_group_role(self, event: AstrMessageEvent) -> str:
        """返回群身份：owner / admin / member。"""
        sender = getattr(event.message_obj, "sender", None)
        role = getattr(sender, "role", None) if sender else None
        if isinstance(role, str) and role.lower() in ("owner", "admin", "member"):
            return role.lower()

        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            raw_role = (raw.get("sender") or {}).get("role")
            if isinstance(raw_role, str) and raw_role.lower() in ("owner", "admin", "member"):
                return raw_role.lower()

        # AstrBot 会把群主/管理都标成 event.role=admin，无法区分时按 admin 处理
        if getattr(event, "role", "") == "admin" or (
            hasattr(event, "is_admin") and callable(event.is_admin) and event.is_admin()
        ):
            return "admin"
        return "member"

    def _check_perm(self, event: AstrMessageEvent, group_id: str, action: str = "操作") -> tuple[bool, str]:
        """按四级权限校验：1仅Bot管理 / 2仅群主 / 3群主+管理 / 4全员。Bot 管理员始终放行。"""
        user_id = str(event.get_sender_id())
        if self.is_bot_admin(user_id):
            return True, ""

        mode = self._get_perm_mode(group_id)
        label = PERM_LEVELS.get(mode, str(mode))
        deny = f"权限不足，当前为「{label}」，无法{action}"

        if mode == 4:
            return True, ""

        role = self._get_group_role(event)
        if mode == 3 and role in ("owner", "admin"):
            return True, ""
        if mode == 2 and role == "owner":
            return True, ""
        # mode == 1：仅 Bot 管理员（上面已放行）
        return False, deny

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

    def _schedule_layout(self, group_id: str, only_day: Optional[int]) -> dict:
        """计算周表画布尺寸。整周图始终留补番栏；今日图只在名额未满时追加。"""
        schedule = self._load_schedule(group_id)
        label_w, poster_h, poster_gap = 88, 220, 10
        row_gap, margin, canvas_w = 8, 12, 1080
        poster_pad_top, title_gap, title_line_h = 8, 6, 22
        ep_line_h, poster_pad_bottom = 18, 8
        row_h = poster_pad_top + poster_h + title_gap + title_line_h + 2 + ep_line_h + poster_pad_bottom
        poster_area_w = canvas_w - margin * 2 - label_w - poster_gap
        day_range = [only_day] if only_day is not None else list(range(1, 8))
        max_posters = max(1, max((len(schedule.get(str(i), [])) for i in day_range), default=1))
        poster_w = max(120, int((poster_area_w - poster_gap * (max_posters - 1)) / max_posters))
        if only_day is None:
            backlog = self._load_backlog(group_id)
            include_backlog = True
        else:
            backlog = self._today_fill_entries(group_id, only_day)
            include_backlog = bool(backlog)
        num_rows = len(day_range) + (1 if include_backlog else 0)
        return {
            "schedule": schedule,
            "backlog": backlog,
            "include_backlog": include_backlog,
            "label_w": label_w,
            "poster_h": poster_h,
            "poster_gap": poster_gap,
            "row_gap": row_gap,
            "margin": margin,
            "canvas_w": canvas_w,
            "poster_pad_top": poster_pad_top,
            "title_gap": title_gap,
            "title_line_h": title_line_h,
            "row_h": row_h,
            "poster_area_w": poster_area_w,
            "day_range": day_range,
            "poster_w": poster_w,
            "canvas_h": margin * 2 + row_h * num_rows + row_gap * max(0, num_rows - 1),
            "backlog_poster_w": self._fit_poster_width(poster_area_w, poster_gap, len(backlog)),
        }

    def _fit_poster_width(self, area_w: int, gap: int, count: int) -> int:
        """让补番封面缩进这一栏的宽度里。"""
        if count <= 1:
            return area_w
        return max(1, int((area_w - gap * (count - 1)) / count))

    def _airing_poster_caption(self, entry: dict) -> tuple[str, tuple]:
        """新番封面下方的进度文案和颜色。"""
        entry = _normalize_entry(entry)
        if _is_paused(entry):
            text, fill = f"停播 {entry['watched_ep']}/{entry['max_ep']}", (170, 170, 180)
        elif _is_finished(entry):
            text, fill = f"完结 {entry['watched_ep']}/{entry['max_ep']}", (255, 209, 102)
        else:
            text = f"已看{entry['watched_ep']}·应看{_should_watch_ep(entry)}/{entry['max_ep']}"
            fill = (114, 208, 255)
        if _is_watched_today(entry) and not _is_paused(entry):
            text = "✓" + text
        return text, fill

    def _draw_row_label(self, draw, font_day, layout: dict, y: int, text: str, fill: tuple, highlight: bool):
        """画左侧色块和栏名。"""
        x_label = layout["margin"]
        label_w, row_h = layout["label_w"], layout["row_h"]
        draw.rectangle([x_label, y, x_label + label_w, y + row_h], fill=fill)
        if highlight:
            draw.rectangle(
                [
                    x_label - 2,
                    y - 2,
                    x_label + label_w + layout["poster_area_w"] + layout["poster_gap"] + 2,
                    y + row_h + 2,
                ],
                outline=(255, 220, 80),
                width=3,
            )
        tw = draw.textlength(text, font=font_day) if hasattr(draw, "textlength") else 56
        draw.text(
            (x_label + (label_w - tw) / 2, y + (row_h - 28) / 2),
            text,
            fill=(30, 30, 30),
            font=font_day,
        )

    def _draw_poster_card(
        self, img, draw, font_title, group_id: str, entry: dict,
        x_poster: int, y: int, poster_w: int, layout: dict, ep_text: str, ep_fill: tuple,
    ):
        """画一张封面、标题和进度。"""
        poster_h = layout["poster_h"]
        poster_pad_top = layout["poster_pad_top"]
        image_name = entry.get("image")
        title = (entry.get("title") or "").strip()
        full_path = os.path.join(self._group_dir(group_id), image_name) if image_name else ""
        if full_path and os.path.isfile(full_path):
            self._paste_poster(img, draw, font_title, full_path, x_poster, y, poster_w, poster_h, poster_pad_top)
        else:
            draw.rectangle(
                [x_poster, y + poster_pad_top, x_poster + poster_w, y + poster_pad_top + poster_h],
                fill=(50, 50, 58),
            )
            draw.text((x_poster + 8, y + poster_pad_top + poster_h // 2), "无封面", fill=(180, 180, 190), font=font_title)
        self._draw_poster_title(draw, font_title, title, x_poster, y, poster_w, layout)
        if font_title and ep_text:
            tw3 = draw.textlength(ep_text, font=font_title) if hasattr(draw, "textlength") else 90
            ep_x = x_poster + max(0, (poster_w - tw3) / 2)
            ep_y = y + poster_pad_top + poster_h + layout["title_gap"] + layout["title_line_h"] + 2
            draw.text((ep_x, ep_y), ep_text, fill=ep_fill, font=font_title)

    def _paste_poster(self, img, draw, font_title, full_path: str, x_poster: int, y: int, poster_w: int, poster_h: int, poster_pad_top: int):
        """把封面图贴进格子，失败时画占位。"""
        try:
            with PILImage.open(full_path) as src:
                src = src.convert("RGB")
                ratio = min(poster_w / max(1, src.width), poster_h / max(1, src.height))
                nw = max(1, int(src.width * ratio))
                nh = max(1, int(src.height * ratio))
                resized = src.resize((nw, nh))
                px = x_poster + (poster_w - nw) // 2
                py = y + poster_pad_top + (poster_h - nh) // 2
                img.paste(resized, (px, py))
        except Exception:
            draw.rectangle(
                [x_poster, y + poster_pad_top, x_poster + poster_w, y + poster_pad_top + poster_h],
                fill=(60, 60, 68),
            )
            draw.text((x_poster + 8, y + poster_pad_top + poster_h // 2), "加载失败", fill=(200, 200, 200), font=font_title)

    def _draw_poster_title(self, draw, font_title, title: str, x_poster: int, y: int, poster_w: int, layout: dict):
        """在封面下方画标题底。"""
        if not title or font_title is None:
            return
        title_show = title if len(title) <= 10 else title[:9] + "…"
        tw2 = draw.textlength(title_show, font=font_title) if hasattr(draw, "textlength") else 80
        title_x = x_poster + max(0, (poster_w - tw2) / 2)
        title_y = y + layout["poster_pad_top"] + layout["poster_h"] + layout["title_gap"]
        title_pad_x, title_pad_y = 4, 2
        draw.rectangle(
            [
                title_x - title_pad_x,
                title_y - title_pad_y,
                title_x + tw2 + title_pad_x,
                title_y + layout["title_line_h"],
            ],
            fill=(35, 35, 42),
        )
        draw.text((title_x, title_y), title_show, fill=(245, 245, 250), font=font_title)

    def _draw_entry_row(
        self, img, draw, font_title, group_id: str, entries: list, y: int,
        poster_w: int, layout: dict, empty_text: str, caption_of,
    ):
        """画一栏里的封面列表。"""
        x_poster = layout["margin"] + layout["label_w"] + layout["poster_gap"]
        if not entries:
            draw.text((x_poster + 12, y + layout["row_h"] // 2 - 10), empty_text, fill=(120, 120, 130), font=font_title)
            return
        for entry in entries:
            ep_text, ep_fill = caption_of(entry)
            self._draw_poster_card(img, draw, font_title, group_id, entry, x_poster, y, poster_w, layout, ep_text, ep_fill)
            x_poster += poster_w + layout["poster_gap"]

    def _backlog_poster_caption(self, entry: dict) -> tuple[str, tuple]:
        """补番封面下方的进度文案和颜色。"""
        text, finished = poster_caption(entry)
        fill = (255, 209, 102) if finished else (198, 168, 230)
        return text, fill

    def _build_schedule_image(
        self,
        group_id: str,
        highlight_day: Optional[int] = None,
        only_day: Optional[int] = None,
    ) -> Optional[str]:
        """生成周表长图。整周图始终带补番栏；今日图只在未看满时追加补番。"""
        if PILImage is None or ImageDraw is None:
            return None
        layout = self._schedule_layout(group_id, only_day)
        img = PILImage.new("RGB", (layout["canvas_w"], layout["canvas_h"]), (18, 18, 20))
        draw = ImageDraw.Draw(img)
        font_day = _pick_cjk_font(28)
        font_title = _pick_cjk_font(16)
        if font_day is None:
            return None

        # 周一到周日
        y = layout["margin"]
        for day_idx in layout["day_range"]:
            self._draw_row_label(draw, font_day, layout, y, DAY_NAMES[day_idx - 1], DAY_COLORS[day_idx - 1], highlight_day == day_idx)
            self._draw_entry_row(
                img, draw, font_title, group_id, layout["schedule"].get(str(day_idx), []),
                y, layout["poster_w"], layout, "（暂无番剧）", self._airing_poster_caption,
            )
            y += layout["row_h"] + layout["row_gap"]

        # 补番栏：整周图始终有；今日图只在观看名额没满时追加
        if layout["include_backlog"]:
            self._draw_row_label(draw, font_day, layout, y, "补番", (176, 146, 196), False)
            self._draw_entry_row(
                img, draw, font_title, group_id, layout["backlog"],
                y, layout["backlog_poster_w"], layout, "（暂无补番）", self._backlog_poster_caption,
            )

        prefix = f"day_{only_day}" if only_day is not None else "schedule"
        out_path = os.path.join(self._group_dir(group_id), f"{prefix}_{int(time.time())}.png")
        img.save(out_path, format="PNG")
        return out_path

    def _format_day_entries_text(self, day_idx: int, entries: list, with_tip: bool = False) -> str:
        if not entries:
            text = f"📺 {DAY_NAMES[day_idx - 1]}：暂无番剧"
        else:
            lines = [f"📺 {DAY_NAMES[day_idx - 1]} 共 {len(entries)} 部："]
            for i, entry in enumerate(entries, 1):
                entry = _normalize_entry(entry)
                title = entry.get("title") or f"番剧#{i}"
                lines.append(f"  {i}. {title} · {_progress_label(entry)}")
            text = "\n".join(lines)
        if with_tip and entries:
            text += (
                "\n————\n"
                "更新进度：已看 周X / 已看 编号 / 已看全部\n"
                "停播跳过：番剧暂停 周X 编号；恢复后才会继续更新已看\n"
                "表情回应：数字表情=对应编号，👍/OK/强=全部看过"
            )
        return text

    def _find_entry(self, group_id: str, day_idx: int, index: int) -> tuple[Optional[dict], Optional[dict], Optional[list]]:
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        if index < 1 or index > len(entries):
            return None, schedule, entries
        return entries[index - 1], schedule, entries

    def _format_progress_result(self, ok: bool, entry: dict, msg: str) -> str:
        """把进度操作结果格式化成提示行。"""
        if ok:
            return msg
        if _is_paused(entry):
            return f"⏸ {msg}"
        return f"❌ {msg}"

    def _resolve_day_index(
        self, day_token: str, index: str
    ) -> tuple[Optional[int], Optional[int], Optional[str]]:
        """
        解析「周X 编号」或「编号」（默认今天）。

        Args:
            day_token: 星期或编号。
            index: 编号；当 day_token 本身是编号时可为空。
        Returns:
            (星期, 编号, 错误说明)；成功时错误说明为 None。
        """
        day_idx = _today_weekday()
        day_token = (day_token or "").strip()
        index = (index or "").strip()
        # 纯数字按编号处理，避免「2」被星期别名收成周二
        if day_token.startswith("周") or day_token.startswith("星期"):
            parsed = _parse_day_token(day_token)
            if not parsed:
                return None, None, f"无法识别星期：{day_token}"
            day_idx = parsed
        elif day_token.isdigit() and not index:
            index = day_token
        elif day_token and not day_token.isdigit() and _parse_day_token(day_token):
            day_idx = _parse_day_token(day_token)
        elif day_token:
            return None, None, f"无法识别星期：{day_token}"
        if not index.isdigit():
            return None, None, "请指定编号"
        return day_idx, int(index), None

    def _apply_pause_state_indices(
        self, group_id: str, day_idx: int, indices: list[int], paused: bool
    ) -> list[str]:
        """按编号将番剧设为停播或恢复。"""
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        messages = []
        changed = False
        action = _pause_one if paused else _resume_one
        for idx in indices:
            if idx < 1 or idx > len(entries):
                messages.append(f"#{idx} 编号无效")
                continue
            ok, msg = action(entries[idx - 1])
            messages.append(msg if ok else f"❌ {msg}")
            if ok:
                changed = True
                title = entries[idx - 1].get("title") or ""
                state = "暂停" if paused else "恢复"
                logger.info(
                    f"番剧{state}: group={group_id} day={day_idx} "
                    f"index={idx} title={title}"
                )
        if changed:
            schedule[day_key] = entries
            self._save_schedule(group_id, schedule)
        return messages

    def _apply_mark_indices(self, group_id: str, day_idx: int, indices: list[int]) -> list[str]:
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        messages = []
        changed = False
        for idx in indices:
            if idx < 1 or idx > len(entries):
                messages.append(f"#{idx} 编号无效")
                continue
            ok, msg = _mark_watched_one(entries[idx - 1])
            messages.append(self._format_progress_result(ok, entries[idx - 1], msg))
            if ok:
                changed = True
        if changed:
            schedule[day_key] = entries
            self._save_schedule(group_id, schedule)
        return messages

    def _apply_mark_day(self, group_id: str, day_idx: int) -> list[str]:
        schedule = self._load_schedule(group_id)
        entries = schedule.get(str(day_idx), [])
        if not entries:
            return [f"{DAY_NAMES[day_idx - 1]} 暂无番剧"]
        indices = list(range(1, len(entries) + 1))
        return self._apply_mark_indices(group_id, day_idx, indices)

    def _apply_mark_all_today(self, group_id: str) -> list[str]:
        return self._apply_mark_day(group_id, _today_weekday())

    def _apply_undo_indices(self, group_id: str, day_idx: int, indices: list[int]) -> list[str]:
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        messages = []
        changed = False
        for idx in indices:
            if idx < 1 or idx > len(entries):
                messages.append(f"#{idx} 编号无效")
                continue
            ok, msg = _undo_watched_one(entries[idx - 1])
            messages.append(self._format_progress_result(ok, entries[idx - 1], msg))
            if ok:
                changed = True
        if changed:
            schedule[day_key] = entries
            self._save_schedule(group_id, schedule)
        return messages

    def _apply_undo_day(self, group_id: str, day_idx: int) -> list[str]:
        schedule = self._load_schedule(group_id)
        entries = schedule.get(str(day_idx), [])
        if not entries:
            return [f"{DAY_NAMES[day_idx - 1]} 暂无番剧"]
        return self._apply_undo_indices(group_id, day_idx, list(range(1, len(entries) + 1)))

    def _apply_set_watched_indices(
        self, group_id: str, day_idx: int, indices: list[int], watched: int
    ) -> list[str]:
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        messages = []
        changed = False
        for idx in indices:
            if idx < 1 or idx > len(entries):
                messages.append(f"#{idx} 编号无效")
                continue
            entry = _normalize_entry(entries[idx - 1])
            ok, msg = _set_watched_one(entry, watched)
            messages.append(self._format_progress_result(ok, entry, msg))
            if ok:
                changed = True
        if changed:
            schedule[day_key] = entries
            self._save_schedule(group_id, schedule)
        return messages

    async def _apply_max_ep_indices(
        self,
        group_id: str,
        day_idx: int,
        indices: list[int],
        value: Optional[str] = None,
    ) -> list[str]:
        """value 为数字则手动设置；为空则 Bangumi 同步。"""
        schedule = self._load_schedule(group_id)
        day_key = str(day_idx)
        entries = schedule.get(day_key, [])
        messages = []
        changed = False
        manual = value is not None and str(value).strip().isdigit()
        for idx in indices:
            if idx < 1 or idx > len(entries):
                messages.append(f"#{idx} 编号无效")
                continue
            entry = _normalize_entry(entries[idx - 1])
            title = entry.get("title") or ""
            if manual:
                max_ep = max(1, min(999, int(value)))
                note = "手动设置"
            else:
                eps, bangumi_id, note = await fetch_bangumi_max_ep(title)
                if bangumi_id:
                    entry["bangumi_id"] = bangumi_id
                if not eps:
                    messages.append(f"❌ 「{title}」{note}")
                    continue
                max_ep = eps
            entry["max_ep"] = max_ep
            entry["watched_ep"] = min(entry["watched_ep"], max_ep)
            messages.append(
                f"「{title}」总集数设为 {max_ep}（{note}），"
                f"已看{entry['watched_ep']} · 应看第{_should_watch_ep(entry)}集"
            )
            changed = True
        if changed:
            schedule[day_key] = entries
            self._save_schedule(group_id, schedule)
        return messages

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
        # 有未完结补番时，紧跟着发一条今日建议
        backlog_text = self._format_today_backlog(group_id, day_idx)
        if backlog_text:
            yield event.plain_result(backlog_text)

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

        can, deny = self._check_perm(event, group_id, "上传番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        m = re.match(r"^/?番剧上传\s+(\S+)\s+(.+)$", msg)
        if not m:
            yield event.plain_result(
                "用法：番剧上传 周X 番剧名称 [总集数] + 图片\n"
                "示例：番剧上传 周一 葬送的芙莉莲（未填集数时自动查 Bangumi）\n"
                "或：番剧上传 周一 葬送的芙莉莲 12（手动指定总集数）"
            )
            return

        day_idx = _parse_day_token(m.group(1))
        title = m.group(2).strip()
        max_ep = DEFAULT_MAX_EP
        manual_max = False
        title_max = re.match(r"^(.+?)\s+(\d{1,3})$", title)
        if title_max:
            title = title_max.group(1).strip()
            max_ep = max(1, min(999, int(title_max.group(2))))
            manual_max = True
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

        bangumi_id = None
        bangumi_note = ""
        if not manual_max:
            yield event.plain_result(f"🔎 正在从 Bangumi 查询「{title}」总集数…")
            eps, bangumi_id, bangumi_note = await fetch_bangumi_max_ep(title)
            if eps:
                max_ep = eps
            else:
                bangumi_note = bangumi_note or "未查到集数"
                bangumi_note += f"，已使用默认 {DEFAULT_MAX_EP} 集"

        entry = {
            "id": str(uuid.uuid4()),
            "title": title,
            "image": image_name,
            "watched_ep": 0,
            "max_ep": max_ep,
            "last_watched_date": None,
            "paused": False,
            "bangumi_id": bangumi_id,
            "added_by": user_id,
            "added_at": int(time.time()),
        }
        schedule.setdefault(day_key, []).append(entry)
        self._save_schedule(group_id, schedule)
        idx = len(schedule[day_key])
        extra = f"\n{bangumi_note}" if bangumi_note else ""
        yield event.chain_result([
            Comp.Reply(id=str(event.message_obj.message_id)),
            Comp.Plain(
                f"✅ 已添加到 {DAY_NAMES[day_idx - 1]}：{title}（#{idx}，"
                f"应看第1集/共{max_ep}集）{extra}"
            ),
        ])

    @filter.command("已看", alias={"/已看", "看了", "/看了", "番剧看了"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_watched(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "更新进度")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        msg = event.message_str.strip()
        msg = re.sub(r"^/?番剧看了\s*", "", msg)
        msg = re.sub(r"^/?看了\s*", "", msg)
        msg = re.sub(r"^/?已看\s*", "", msg).strip()

        if not msg or msg in ("全部", "全看了", "今日全部", "今天全部"):
            if not msg and not self._is_reply_to_push(event, group_id):
                yield event.plain_result(
                    "用法：\n"
                    "  已看 周X          当天全部标记看过\n"
                    "  已看 周X 编号     指定番剧\n"
                    "  已看 编号         默认今天\n"
                    "  已看全部\n"
                    "示例：已看 周一  /  已看 周一 1  /  已看全部"
                )
                return
            messages = self._apply_mark_all_today(group_id)
            yield event.plain_result("✅ 已更新今日进度：\n" + "\n".join(messages))
            return

        day_idx = _today_weekday()
        tokens = msg.split()
        if tokens:
            first = tokens[0]
            if first.startswith("周") or first.startswith("星期"):
                parsed = _parse_day_token(first)
                if parsed:
                    day_idx = parsed
                    tokens = tokens[1:]
            elif not first.isdigit() and _parse_day_token(first):
                day_idx = _parse_day_token(first)
                tokens = tokens[1:]

        # 只写了周X：对该天全部操作
        if not tokens:
            messages = self._apply_mark_day(group_id, day_idx)
            yield event.plain_result(
                f"✅ 已更新 {DAY_NAMES[day_idx - 1]} 全部进度：\n" + "\n".join(messages)
            )
            return

        indices: list[int] = []
        for tok in tokens:
            if tok.isdigit():
                indices.append(int(tok))
                continue
            schedule = self._load_schedule(group_id)
            entries = schedule.get(str(day_idx), [])
            found = False
            for i, entry in enumerate(entries, 1):
                title = entry.get("title") or ""
                if tok == title or tok in title or title in tok:
                    indices.append(i)
                    found = True
                    break
            if not found:
                yield event.plain_result(f"找不到番剧：{tok}")
                return
        if not indices:
            yield event.plain_result("请指定编号，如：已看 1  或  已看 周一 1；只写「已看 周一」可当天全部")
            return
        messages = self._apply_mark_indices(group_id, day_idx, indices)
        yield event.plain_result("✅ 进度已更新：\n" + "\n".join(messages))

    @filter.command("已看全部", alias={"/已看全部", "看了全部", "/看了全部", "今日全看了", "全看了"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_watched_all(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "更新进度")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return
        messages = self._apply_mark_all_today(group_id)
        yield event.plain_result("✅ 已更新今日进度：\n" + "\n".join(messages))

    @filter.command("撤回", alias={"/撤回", "番剧撤回"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_undo_watched(self, event: AstrMessageEvent, day_token: str = "", index: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "撤回进度")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        day_idx = _today_weekday()
        day_token = (day_token or "").strip()
        index = (index or "").strip()

        # 撤回 周一 → 当天全部
        if day_token and _parse_day_token(day_token) and not index:
            day_idx = _parse_day_token(day_token)
            messages = self._apply_undo_day(group_id, day_idx)
            yield event.plain_result(
                f"✅ 已撤回 {DAY_NAMES[day_idx - 1]} 全部进度：\n" + "\n".join(messages)
            )
            return

        if day_token and day_token.isdigit() and not index:
            index = day_token
        elif _parse_day_token(day_token):
            day_idx = _parse_day_token(day_token)
        if not index.isdigit():
            yield event.plain_result(
                "用法：\n"
                "  撤回 周X        当天全部各撤回一集\n"
                "  撤回 周X 编号   指定番剧\n"
                "  撤回 编号       默认今天"
            )
            return
        idx = int(index)
        messages = self._apply_undo_indices(group_id, day_idx, [idx])
        if len(messages) == 1 and messages[0].startswith("❌"):
            yield event.plain_result(messages[0])
        else:
            yield event.plain_result("✅ " + "\n".join(messages))

    @filter.command("番剧已看", alias={"/番剧已看"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_set_watched(
        self, event: AstrMessageEvent, day_token: str = "", index: str = "", value: str = ""
    ):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "设置进度")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return
        day_idx = _parse_day_token(day_token)
        index = (index or "").strip()
        value = (value or "").strip()

        # 番剧已看 周一 5 → 当天全部设为已看 5
        if day_idx and index.isdigit() and not value:
            watched = int(index)
            schedule = self._load_schedule(group_id)
            entries = schedule.get(str(day_idx), [])
            if not entries:
                yield event.plain_result(f"{DAY_NAMES[day_idx - 1]} 暂无番剧")
                return
            messages = self._apply_set_watched_indices(
                group_id, day_idx, list(range(1, len(entries) + 1)), watched
            )
            yield event.plain_result(
                f"✅ 已设置 {DAY_NAMES[day_idx - 1]} 全部已看为 {watched}：\n"
                + "\n".join(messages)
            )
            return

        if not day_idx or not index.isdigit() or not value.isdigit():
            yield event.plain_result(
                "用法：\n"
                "  番剧已看 周X N         当天全部设为已看 N 集\n"
                "  番剧已看 周X 编号 N    指定番剧\n"
                "示例：番剧已看 周一 3  /  番剧已看 周一 1 3"
            )
            return
        idx = int(index)
        messages = self._apply_set_watched_indices(group_id, day_idx, [idx], int(value))
        yield event.plain_result("✅ " + "\n".join(messages))

    @filter.command("番剧上限", alias={"/番剧上限"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_set_max_ep(
        self, event: AstrMessageEvent, day_token: str = "", index: str = "", value: str = ""
    ):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "设置上限")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return
        day_idx = _parse_day_token(day_token)
        index = (index or "").strip()
        value = (value or "").strip()
        if not day_idx:
            yield event.plain_result(
                "用法：\n"
                "  番剧上限 周X           当天全部从 Bangumi 同步\n"
                "  番剧上限 周X 编号      指定番剧同步 Bangumi\n"
                "  番剧上限 周X 编号 N    指定番剧手动设置\n"
                "  番剧上限 周X 全部 N    当天全部手动设为 N\n"
                "示例：番剧上限 周一  /  番剧上限 周一 1  /  番剧上限 周一 全部 12"
            )
            return

        schedule = self._load_schedule(group_id)
        entries = schedule.get(str(day_idx), [])
        if not entries:
            yield event.plain_result(f"{DAY_NAMES[day_idx - 1]} 暂无番剧")
            return

        # 番剧上限 周一 → 当天全部 Bangumi
        if not index:
            yield event.plain_result(
                f"🔎 正在为 {DAY_NAMES[day_idx - 1]} 共 {len(entries)} 部同步 Bangumi 总集数…"
            )
            messages = await self._apply_max_ep_indices(
                group_id, day_idx, list(range(1, len(entries) + 1)), None
            )
            yield event.plain_result(
                f"✅ {DAY_NAMES[day_idx - 1]} 上限已更新：\n" + "\n".join(messages)
            )
            return

        # 番剧上限 周一 全部 12 → 当天全部手动
        if index in ("全部", "all", "ALL") and value.isdigit():
            messages = await self._apply_max_ep_indices(
                group_id, day_idx, list(range(1, len(entries) + 1)), value
            )
            yield event.plain_result(
                f"✅ {DAY_NAMES[day_idx - 1]} 上限已设为 {value}：\n" + "\n".join(messages)
            )
            return

        if not index.isdigit():
            yield event.plain_result(
                "用法：番剧上限 周X  /  番剧上限 周X 编号  /  番剧上限 周X 编号 N  /  番剧上限 周X 全部 N"
            )
            return

        idx = int(index)
        if idx < 1 or idx > len(entries):
            yield event.plain_result(
                f"编号超出范围，{DAY_NAMES[day_idx - 1]} 当前共 {len(entries)} 部"
            )
            return
        if not value:
            yield event.plain_result(f"🔎 正在从 Bangumi 查询「{entries[idx - 1].get('title', '')}」…")
        messages = await self._apply_max_ep_indices(group_id, day_idx, [idx], value or None)
        yield event.plain_result("✅ " + "\n".join(messages))

    @filter.command("番剧暂停", alias={"/番剧暂停", "暂停番剧", "停播", "番剧停播"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_pause(self, event: AstrMessageEvent, day_token: str = "", index: str = ""):
        """将指定番剧标记为停播，后续批量更新已看时跳过。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "暂停番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        # 解析目标星期和编号
        day_idx, idx, err = self._resolve_day_index(day_token, index)
        if err or day_idx is None or idx is None:
            yield event.plain_result(
                "用法：\n"
                "  番剧暂停 周X 编号\n"
                "  番剧暂停 编号       默认今天\n"
                "示例：番剧暂停 周一 2  /  番剧暂停 2"
            )
            return

        # 写入停播标记
        messages = self._apply_pause_state_indices(group_id, day_idx, [idx], True)
        if len(messages) == 1 and messages[0].startswith("❌"):
            yield event.plain_result(messages[0])
        else:
            yield event.plain_result("✅ " + "\n".join(messages))

    @filter.command("番剧恢复", alias={"/番剧恢复", "恢复番剧", "复播", "番剧复播"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_resume(self, event: AstrMessageEvent, day_token: str = "", index: str = ""):
        """恢复停播番剧，之后可以继续更新已看。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "恢复番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        # 解析目标星期和编号
        day_idx, idx, err = self._resolve_day_index(day_token, index)
        if err or day_idx is None or idx is None:
            yield event.plain_result(
                "用法：\n"
                "  番剧恢复 周X 编号\n"
                "  番剧恢复 编号       默认今天\n"
                "示例：番剧恢复 周一 2  /  番剧恢复 2"
            )
            return

        # 清除停播标记
        messages = self._apply_pause_state_indices(group_id, day_idx, [idx], False)
        if len(messages) == 1 and messages[0].startswith("❌"):
            yield event.plain_result(messages[0])
        else:
            yield event.plain_result("✅ " + "\n".join(messages))

    @filter.command("补番上传", alias={"/补番上传", "上传补番"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_backlog_upload(self, event: AstrMessageEvent):
        """把一部或多部老番加入补番队列。图片只在上传一部时保存。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "上传补番")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        body = re.sub(r"^/?(?:补番上传|上传补番)\s*", "", event.message_str.strip()).strip()
        items = parse_upload_block(body)
        if not items:
            yield event.plain_result(
                "用法：补番上传 名称 [总集数] [已看]\n"
                "多部用换行或 | 分隔，例如：\n"
                "补番上传 钢之炼金术师 64 20 | 进击的巨人 87\n"
                "未写总集数时自动查 Bangumi。一次上传一部时可附封面图。"
            )
            return

        # 只给单条上传保存封面，批量文字不绑定同一张图
        image_name = None
        if len(items) == 1:
            image_name = await self._optional_upload_image(event, group_id)
        if any(max_ep is None for _title, max_ep, _watched in items):
            yield event.plain_result("🔎 正在查询未填写总集数的补番…")

        entries = self._load_backlog(group_id)
        lines = []
        added = 0
        for title, max_ep, watched in items:
            entry, line = await self._build_backlog_entry(
                event, title, max_ep, watched, image_name if added == 0 else None
            )
            lines.append(line)
            if entry:
                entries.append(entry)
                added += 1
            elif image_name:
                self._remove_image_file(group_id, image_name)
                image_name = None
        if added:
            self._save_backlog(group_id, sink_finished(entries))
            logger.info(f"补番上传: group={group_id} added={added}")
        yield event.plain_result("\n".join(lines))

    async def _optional_upload_image(self, event: AstrMessageEvent, group_id: str) -> Optional[str]:
        """有图就保存，没有图也不报错。"""
        image_comp = next((p for p in (event.message_obj.message or []) if isinstance(p, Image)), None)
        reply_image, _reply_text = await self._extract_image_from_reply(event, group_id)
        file_id = image_comp.file if image_comp else None
        image_name = await self.download_image(event, group_id, file_id=file_id, image_comp=image_comp)
        return image_name or reply_image

    async def _build_backlog_entry(
        self,
        event: AstrMessageEvent,
        title: str,
        max_ep: Optional[int],
        watched: Optional[int],
        image_name: Optional[str],
    ) -> tuple[Optional[dict], str]:
        """解析总集数和已看集数，成功时返回可入库的条目。"""
        bangumi_id = None
        note = ""
        if max_ep is None:
            eps, bangumi_id, note = await fetch_bangumi_max_ep(title)
            if eps:
                max_ep = eps
            else:
                max_ep = DEFAULT_MAX_EP
                note = (note or "未查到集数") + f"，已使用默认 {DEFAULT_MAX_EP} 集"
        else:
            max_ep = max(1, min(999, int(max_ep)))
        watched_ep = 0 if watched is None else max(0, int(watched))
        if watched_ep > max_ep:
            return None, f"❌ 「{title}」已看 {watched_ep} 超过总集数 {max_ep}"
        entry = normalize_backlog_entry({
            "id": str(uuid.uuid4()),
            "title": title,
            "image": image_name or "",
            "watched_ep": watched_ep,
            "max_ep": max_ep,
            "logs": [],
            "bangumi_id": bangumi_id,
            "added_by": str(event.get_sender_id()),
            "added_at": int(time.time()),
        })
        extra = f"（{note}）" if note else ""
        return entry, f"✅ 已加入补番：{title}（已看 {watched_ep}/{max_ep}）{extra}"

    @filter.command("补番", alias={"/补番", "补番列表", "/补番列表"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_backlog_list(self, event: AstrMessageEvent):
        """查看完整补番队列。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        yield event.plain_result(format_full_list(self._load_backlog(group_id)))

    @filter.command("补了", alias={"/补了"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_backlog_watched(self, event: AstrMessageEvent):
        """补了 编号 为 +1；补了 编号 N 把今天改成 N 集。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "更新补番")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        tokens = re.sub(r"^/?补了\s*", "", event.message_str.strip()).split()
        if not tokens or not tokens[0].isdigit() or (len(tokens) > 1 and not tokens[1].isdigit()):
            yield event.plain_result(
                "用法：\n"
                "  补了 编号        今天 +1 集\n"
                "  补了 编号 N      把今天改成 N 集\n"
                "示例：补了 1  /  补了 2 3"
            )
            return

        index = int(tokens[0])
        if len(tokens) > 1 and int(tokens[1]) < 1:
            yield event.plain_result("今天的集数至少为 1。撤回请用「补番撤回 编号」")
            return
        if len(tokens) == 1:
            message = self._update_backlog_entry(group_id, index, lambda entry: add_today(entry, 1))
        else:
            count = int(tokens[1])
            message = self._update_backlog_entry(
                group_id, index, lambda entry: set_today_count(entry, count)
            )
        yield event.plain_result(message if message.startswith("❌") else "✅ " + message)

    @filter.command("补番已看", alias={"/补番已看"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_backlog_set_watched(
        self, event: AstrMessageEvent, index: str = "", value: str = ""
    ):
        """直接设置补番总进度，用于纠错。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "设置补番进度")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return
        index = (index or "").strip()
        value = (value or "").strip()
        if not index.isdigit() or not value.isdigit():
            yield event.plain_result(
                "用法：补番已看 编号 N\n"
                "示例：补番已看 1 20（把第 1 部直接设为已看 20 集）"
            )
            return
        watched = int(value)
        message = self._update_backlog_entry(
            group_id, int(index), lambda entry: set_watched_total(entry, watched)
        )
        yield event.plain_result(message if message.startswith("❌") else "✅ " + message)

    @filter.command("补番撤回", alias={"/补番撤回"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_backlog_undo(self, event: AstrMessageEvent, index: str = ""):
        """撤回指定补番今天的 1 集。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = await self._check_perm_async(event, group_id, "撤回补番")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return
        if not str(index).strip().isdigit():
            yield event.plain_result("用法：补番撤回 编号")
            return
        message = self._update_backlog_entry(group_id, int(index), undo_today)
        yield event.plain_result(message if message.startswith("❌") else "✅ " + message)

    @filter.command("补番删除", alias={"/补番删除", "删除补番", "/删除补番"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_backlog_delete(self, event: AstrMessageEvent, index: str = ""):
        """把指定补番移出队列，或一次清掉已完结的。"""
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "删除补番")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return
        token = (index or "").strip()
        if not token:
            yield event.plain_result(
                "用法：\n"
                "  补番删除 编号     删除指定一部\n"
                "  补番删除 完结     删除队列里所有已看完的\n"
                "先发「补番」查看编号；已完结的会排在列表后面。"
            )
            return

        entries = self._load_backlog(group_id)
        if token in ("完结", "已完结", "finished"):
            kept = []
            removed_titles = []
            for entry in entries:
                if entry["watched_ep"] >= entry["max_ep"]:
                    self._remove_image_file(group_id, entry.get("image"))
                    removed_titles.append(entry.get("title") or "补番")
                else:
                    kept.append(entry)
            if not removed_titles:
                yield event.plain_result("当前没有已完结的补番可删")
                return
            self._save_backlog(group_id, kept)
            logger.info(f"补番删除完结: group={group_id} count={len(removed_titles)}")
            yield event.plain_result(
                f"✅ 已删除 {len(removed_titles)} 部完结补番：\n"
                + "\n".join(f"  · {title}" for title in removed_titles)
            )
            return

        if not token.isdigit():
            yield event.plain_result("用法：补番删除 编号  或  补番删除 完结")
            return
        idx = int(token)
        err = self._backlog_index_error(entries, idx)
        if err:
            yield event.plain_result(f"❌ {err}")
            return
        removed = entries.pop(idx - 1)
        self._remove_image_file(group_id, removed.get("image"))
        self._save_backlog(group_id, entries)
        logger.info(f"补番删除: group={group_id} index={idx} title={removed.get('title', '')}")
        yield event.plain_result(f"✅ 已从补番队列删除 #{idx}：{removed.get('title', '')}")

    def _update_backlog_entry(self, group_id: str, index: int, mutator) -> str:
        """更新一条补番并在完结后把该条沉到队尾。"""
        entries = self._load_backlog(group_id)
        err = self._backlog_index_error(entries, index)
        if err:
            return f"❌ {err}"
        entry = entries[index - 1]
        title = entry.get("title") or ""
        ok, msg = mutator(entry)
        if not ok:
            return f"❌ {msg}"
        self._save_backlog(group_id, sink_finished(entries))
        logger.info(f"补番进度: group={group_id} index={index} title={title} detail={msg}")
        return msg

    def _is_reply_to_push(self, event: AstrMessageEvent, group_id: str) -> bool:
        settings = self._load_settings(group_id)
        push_ids = {str(x) for x in (settings.get("last_push_message_ids") or [])}
        if not push_ids:
            return False
        reply_comp = next(
            (m for m in (event.message_obj.message or []) if isinstance(m, Reply)), None
        )
        if not reply_comp:
            return False
        return str(reply_comp.id) in push_ids

    def _collect_reaction_emoji_ids(self, raw: dict) -> list[str]:
        """从表情回应通知里取出 emoji_id。"""
        emoji_ids = []
        likes = raw.get("likes")
        if isinstance(likes, list):
            for item in likes:
                if isinstance(item, dict) and item.get("emoji_id") is not None:
                    emoji_ids.append(str(item.get("emoji_id")))
        if raw.get("emoji_id") is not None:
            emoji_ids.append(str(raw.get("emoji_id")))
        return emoji_ids

    async def _apply_backlog_reaction(self, event: AstrMessageEvent, group_id: str, raw: dict, emoji_ids: list):
        """补番推送上的数字表情：一部时表示集数，多部时表示编号。"""
        numbers = [NUM_REACTION_IDS[eid] for eid in emoji_ids if eid in NUM_REACTION_IDS]
        if not numbers:
            yield event.plain_result("📚 补番请贴数字表情。只有一部时 1/2/3 表示今天看了几集；多部时数字是编号。")
            return
        entries = self._load_backlog(group_id)
        airing_count = self._airing_slot_count(group_id)
        quota = self._watch_quota(group_id)
        removing = raw.get("is_add") is False
        action = undo_emoji if removing else apply_emoji
        messages = []
        changed = False
        for number in numbers:
            entries, ok, msg = action(entries, number, airing_count, quota)
            messages.append(msg if ok else f"❌ {msg}")
            if ok:
                changed = True
        if changed:
            self._save_backlog(group_id, entries)
            logger.info(
                f"补番表情: group={group_id} numbers={numbers} remove={removing}"
            )
        prefix = "📚 补番表情：已撤回\n" if removing else "📚 补番表情：进度已更新\n"
        yield event.plain_result(prefix + "\n".join(messages))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_reaction(self, event: AstrMessageEvent):
        """监听 NapCat 等协议的群消息表情回应，更新今日进度。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice":
            return
        notice_type = str(raw.get("notice_type") or "")
        is_reaction = (
            notice_type in ("group_msg_emoji_like", "group_msg_reaction", "reaction")
            or raw.get("sub_type") in ("emoji_like", "reaction")
            or "likes" in raw
        )
        if not is_reaction:
            return

        group_id = str(raw.get("group_id") or event.get_group_id() or "")
        if not group_id:
            return
        settings = self._load_settings(group_id)
        push_ids = {str(x) for x in (settings.get("last_push_message_ids") or [])}
        backlog_ids = {str(x) for x in (settings.get("last_backlog_message_ids") or [])}
        target_mid = str(raw.get("message_id") or "")
        is_backlog = bool(backlog_ids) and target_mid in backlog_ids
        is_airing = bool(push_ids) and target_mid in push_ids
        if not is_backlog and not is_airing:
            return
        if settings.get("last_push_date") != _today_date_str():
            return
        # 新番消息取消表情不回退；补番消息取消表情收回今天 1 集
        if raw.get("is_add") is False and not is_backlog:
            return

        can, deny = await self._check_perm_async(event, group_id, "更新进度")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        emoji_ids = self._collect_reaction_emoji_ids(raw)
        if not emoji_ids:
            return
        if is_backlog:
            async for result in self._apply_backlog_reaction(event, group_id, raw, emoji_ids):
                yield result
            return

        day_idx = int(settings.get("last_push_day") or _today_weekday())
        indices: list[int] = []
        mark_all = False
        done_set = {str(x) for x in DONE_REACTION_IDS}
        for eid in emoji_ids:
            if eid in done_set:
                mark_all = True
            elif eid in NUM_REACTION_IDS:
                indices.append(NUM_REACTION_IDS[eid])

        if mark_all:
            messages = self._apply_mark_all_today(group_id)
            yield event.plain_result("📺 表情回应：已全部标记看过\n" + "\n".join(messages))
            return
        if indices:
            messages = self._apply_mark_indices(group_id, day_idx, indices)
            yield event.plain_result("📺 表情回应：进度已更新\n" + "\n".join(messages))

    @filter.command("删除番剧", alias={"/删除番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_delete_one(self, event: AstrMessageEvent, day_token: str = "", index: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "删除番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
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

    @filter.command("移动番剧", alias={"/移动番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_move(self, event: AstrMessageEvent, from_day: str = "", index: str = "", to_day: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "移动番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        from_idx = _parse_day_token(from_day)
        to_idx = _parse_day_token(to_day)
        if not from_idx or not to_idx or not str(index).strip().isdigit():
            yield event.plain_result("用法：移动番剧 周X 编号 周Y（如 移动番剧 周一 1 周三）")
            return
        if from_idx == to_idx:
            yield event.plain_result("源星期与目标星期相同，无需移动")
            return

        idx = int(index)
        schedule = self._load_schedule(group_id)
        from_key = str(from_idx)
        to_key = str(to_idx)
        entries = schedule.get(from_key, [])
        if idx < 1 or idx > len(entries):
            yield event.plain_result(f"编号超出范围，{DAY_NAMES[from_idx - 1]} 当前共 {len(entries)} 部")
            return

        settings = self._load_settings(group_id)
        max_per_day = max(1, min(20, int(settings.get("max_per_day", self.max_per_day_default))))
        if len(schedule.get(to_key, [])) >= max_per_day:
            yield event.plain_result(
                f"❌ {DAY_NAMES[to_idx - 1]} 已达上限（{max_per_day} 部），请先删除或清空"
            )
            return

        moved = entries.pop(idx - 1)
        schedule[from_key] = entries
        schedule.setdefault(to_key, []).append(moved)
        self._save_schedule(group_id, schedule)
        new_idx = len(schedule[to_key])
        yield event.plain_result(
            f"✅ 已将 {DAY_NAMES[from_idx - 1]} #{idx}「{moved.get('title', '')}」"
            f"移动到 {DAY_NAMES[to_idx - 1]}（#{new_idx}）"
        )

    @filter.command("交换番剧", alias={"/交换番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_swap(
        self,
        event: AstrMessageEvent,
        day_a: str = "",
        index_a: str = "",
        day_b: str = "",
        index_b: str = "",
    ):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "交换番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        a_day = _parse_day_token(day_a)
        b_day = _parse_day_token(day_b)
        if (
            not a_day
            or not b_day
            or not str(index_a).strip().isdigit()
            or not str(index_b).strip().isdigit()
        ):
            yield event.plain_result(
                "用法：交换番剧 周X 编号A 周Y 编号B（如 交换番剧 周一 1 周三 2）"
            )
            return

        a_idx = int(index_a)
        b_idx = int(index_b)
        if a_day == b_day and a_idx == b_idx:
            yield event.plain_result("两部番剧相同，无需交换")
            return

        schedule = self._load_schedule(group_id)
        a_key = str(a_day)
        b_key = str(b_day)
        a_entries = schedule.get(a_key, [])
        b_entries = schedule.get(b_key, [])

        if a_idx < 1 or a_idx > len(a_entries):
            yield event.plain_result(
                f"编号超出范围，{DAY_NAMES[a_day - 1]} 当前共 {len(a_entries)} 部"
            )
            return
        if b_idx < 1 or b_idx > len(b_entries):
            yield event.plain_result(
                f"编号超出范围，{DAY_NAMES[b_day - 1]} 当前共 {len(b_entries)} 部"
            )
            return

        title_a = a_entries[a_idx - 1].get("title", "")
        title_b = b_entries[b_idx - 1].get("title", "")
        a_entries[a_idx - 1], b_entries[b_idx - 1] = b_entries[b_idx - 1], a_entries[a_idx - 1]
        schedule[a_key] = a_entries
        schedule[b_key] = b_entries
        self._save_schedule(group_id, schedule)
        yield event.plain_result(
            f"✅ 已交换：\n"
            f"  {DAY_NAMES[a_day - 1]} #{a_idx}「{title_a}」 ↔ "
            f"{DAY_NAMES[b_day - 1]} #{b_idx}「{title_b}」"
        )

    @filter.command("清空番剧", alias={"/清空番剧"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_clear(self, event: AstrMessageEvent, target: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "清空番剧")
        if not can:
            yield event.plain_result(f"❌ {deny}")
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
        msg = event.message_str.strip()
        m = re.search(r"(\d+)", msg)
        settings = self._load_settings(group_id)
        current = self._get_perm_mode(group_id)
        help_lines = "\n".join(f"{k}：{v}" for k, v in PERM_LEVELS.items())
        if not m:
            yield event.plain_result(
                f"当前操作权限：{current}（{PERM_LEVELS[current]}）\n"
                f"{help_lines}\n"
                "设置示例：番剧权限3\n"
                "说明：该权限同时作用于上传、删除、移动、交换、清空、设置与推送管理"
            )
            return

        can, deny = self._check_perm(event, group_id, "修改权限")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        mode = int(m.group(1))
        if mode not in PERM_LEVELS:
            yield event.plain_result("模式只能是 1、2、3 或 4")
            return
        settings["perm_mode"] = mode
        self._save_settings(group_id, settings)
        yield event.plain_result(f"✅ 番剧操作权限已设为：{mode}（{PERM_LEVELS[mode]}）")

    def _save_watch_quota(self, group_id: str, settings: dict, value: str) -> str:
        """保存观看名额。这个数字不限制每天能添加几部番。"""
        if not str(value).strip().isdigit():
            return "用法：番剧设置 观看名额 3（1~10）。只用于计算今天要不要补番，不影响每日上限。"
        quota = self._clamp_watch_quota(value, self.watch_quota_default)
        if int(value) != quota:
            return "观看名额范围是 1~10，例如：番剧设置 观看名额 3"
        settings["watch_quota"] = quota
        self._save_settings(group_id, settings)
        logger.info(f"观看名额: group={group_id} quota={quota}")
        return (
            f"✅ 观看名额已设为 {quota}。"
            "未看满时今日番剧会用补番补位，停播的番不计入；每天能添加几部仍由「每日上限」决定。"
        )

    @filter.command("番剧设置")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_settings(self, event: AstrMessageEvent, feature: str = "", value: str = ""):
        event.call_llm = True
        group_id = str(event.get_group_id())
        can, deny = self._check_perm(event, group_id, "修改设置")
        if not can:
            yield event.plain_result(f"❌ {deny}")
            return

        settings = self._load_settings(group_id)
        feature = (feature or "").strip()
        if not feature:
            push_enabled = bool(settings.get("push_enabled"))
            push_hour = int(settings.get("push_hour", self.push_hour_default))
            push_minute = int(settings.get("push_minute", self.push_minute_default))
            perm = self._get_perm_mode(group_id)
            yield event.plain_result(
                f"番剧设置\n"
                f"每日上限：{settings.get('max_per_day', self.max_per_day_default)}（每天可添加的数量）\n"
                f"观看名额：{self._watch_quota(group_id)}（未看满时用补番补位，停播不计入）\n"
                f"操作权限：{perm}（{PERM_LEVELS[perm]}）\n"
                f"定时推送：{'开启' if push_enabled else '关闭'}（{self._format_push_time(push_hour, push_minute)}）\n"
                "设置示例：番剧设置 每日上限 10  /  番剧设置 观看名额 3"
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

        if feature in ("观看名额", "每日番剧名额", "补番名额"):
            message = self._save_watch_quota(group_id, settings, value)
            yield event.plain_result(message)
            return

        yield event.plain_result("未知设置项，目前支持：每日上限、观看名额")

    @filter.command("番剧推送", alias={"/番剧推送"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_push(self, event: AstrMessageEvent):
        event.call_llm = True
        group_id = str(event.get_group_id())
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
            can, deny = self._check_perm(event, group_id, "设置定时推送")
            if not can:
                yield event.plain_result(f"❌ {deny}")
                return
            settings["push_enabled"] = True
            settings["push_umo"] = event.unified_msg_origin
            self._save_settings(group_id, settings)
            hour = int(settings.get("push_hour", self.push_hour_default))
            minute = int(settings.get("push_minute", self.push_minute_default))
            yield event.plain_result(
                f"✅ 已开启本群定时推送，每天 {self._format_push_time(hour, minute)} 自动发送今日番剧；有补番时会再发一条"
            )
            return

        if msg in ("番剧推送 关闭", "/番剧推送 关闭"):
            can, deny = self._check_perm(event, group_id, "设置定时推送")
            if not can:
                yield event.plain_result(f"❌ {deny}")
                return
            settings["push_enabled"] = False
            self._save_settings(group_id, settings)
            yield event.plain_result("✅ 已关闭本群定时推送")
            return

        if msg.startswith("番剧推送 时间") or msg.startswith("/番剧推送 时间"):
            can, deny = self._check_perm(event, group_id, "设置定时推送")
            if not can:
                yield event.plain_result(f"❌ {deny}")
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
            settings["last_scheduled_push_date"] = None
            self._save_settings(group_id, settings)
            yield event.plain_result(f"✅ 定时推送时间已设为每天 {self._format_push_time(hour, minute)}")
            return

        if msg in ("番剧推送 立即", "/番剧推送 立即"):
            can, deny = self._check_perm(event, group_id, "测试推送")
            if not can:
                yield event.plain_result(f"❌ {deny}")
                return
            umo = settings.get("push_umo") or event.unified_msg_origin
            bot = getattr(event, "bot", None)
            # 立即推送不占用「今日定时已发」标记，晚上定时仍会再推
            ok = await self._send_today_push(
                group_id, umo, header="📺 今日番剧（手动测试推送）", bot=bot, mark_scheduled=False
            )
            if ok:
                yield event.plain_result("✅ 测试推送已发送")
            else:
                yield event.plain_result("❌ 推送失败，请检查 Bot 是否有主动发消息权限")
            return

        yield event.plain_result("未知子命令，发送「番剧推送」查看帮助")
