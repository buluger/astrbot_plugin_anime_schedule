"""补番队列的进度、今日集数与每日建议。"""

from __future__ import annotations

import re
from datetime import date, datetime

DEFAULT_MAX_EP = 12
DAILY_WATCH_QUOTA = 3
EMOJI_TODAY_MAX = 3


def _today_date_str() -> str:
    """返回今天的日期字符串。"""
    return datetime.now().strftime("%Y-%m-%d")


def _coerce_count(value, default: int = 0) -> int:
    """把外部输入收成非负整数。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_backlog_entry(entry: dict, today: str | None = None) -> dict:
    """
    补齐补番条目的集数与按日记录。

    Args:
        entry: 补番条目。
        today: 用于过滤非法日期的参照日，默认今天。
    Returns:
        原地补齐后的条目；非 dict 则原样返回。
    """
    if not isinstance(entry, dict):
        return entry
    max_ep = _coerce_count(entry.get("max_ep", DEFAULT_MAX_EP), DEFAULT_MAX_EP)
    max_ep = max(1, min(999, max_ep or DEFAULT_MAX_EP))
    watched = min(_coerce_count(entry.get("watched_ep", 0)), max_ep)
    entry["max_ep"] = max_ep
    entry["watched_ep"] = watched
    entry["title"] = str(entry.get("title") or "").strip()
    entry["logs"] = _normalize_logs(entry.get("logs"), today or _today_date_str(), max_ep)
    return entry


def _normalize_logs(raw, today: str, max_ep: int) -> list[dict]:
    """合并同一天的记录，丢掉非法日期。"""
    merged: dict[str, int] = {}
    if not isinstance(raw, list):
        return []
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = str(item.get("date") or "")
        if not _is_iso_date(day):
            continue
        count = _coerce_count(item.get("count", 0))
        if count <= 0:
            continue
        merged[day] = merged.get(day, 0) + count
    logs = [{"date": day, "count": min(count, max_ep)} for day, count in merged.items()]
    logs.sort(key=lambda item: item["date"])
    return logs


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_finished(entry: dict) -> bool:
    """判断补番是否已看到总集数。"""
    entry = normalize_backlog_entry(entry)
    return entry["watched_ep"] >= entry["max_ep"]


def today_count(entry: dict, today: str | None = None) -> int:
    """读取某一天已经记下的补番集数。"""
    entry = normalize_backlog_entry(entry, today)
    day = today or _today_date_str()
    for item in entry["logs"]:
        if item["date"] == day:
            return int(item["count"])
    return 0


def week_count(entry: dict, today: str | None = None) -> int:
    """汇总与 today 同一个周一至周日的补番集数。"""
    entry = normalize_backlog_entry(entry, today)
    day = today or _today_date_str()
    total = 0
    for item in entry["logs"]:
        if _in_same_week(item["date"], day):
            total += int(item["count"])
    return total


def _in_same_week(left: str, right: str) -> bool:
    a = date.fromisoformat(left)
    b = date.fromisoformat(right)
    return a.isocalendar()[:2] == b.isocalendar()[:2]


def poster_caption(entry: dict, today: str | None = None) -> tuple[str, bool]:
    """
    周表补番栏用的短进度。

    Args:
        entry: 补番条目。
        today: 统计「今日」的日期，默认今天。
    Returns:
        (文案, 是否已完结)。
    """
    entry = normalize_backlog_entry(entry, today)
    watched = entry["watched_ep"]
    max_ep = entry["max_ep"]
    if is_finished(entry):
        return f"完结 {watched}/{max_ep}", True
    text = f"已看{watched}/{max_ep}"
    current = today_count(entry, today)
    if current:
        text = f"今日+{current}·{text}"
    return text, False


def progress_label(entry: dict, today: str | None = None) -> str:
    """生成列表用的进度文案。"""
    entry = normalize_backlog_entry(entry, today)
    watched = entry["watched_ep"]
    max_ep = entry["max_ep"]
    if is_finished(entry):
        head = f"已看{watched}集 · 已完结/{max_ep}"
    else:
        head = f"已看{watched}/{max_ep}"
    return (
        f"{head} · 今日+{today_count(entry, today)}"
        f" · 本周+{week_count(entry, today)}"
    )


def set_today_count(entry: dict, count: int, today: str | None = None) -> tuple[bool, str]:
    """
    把某一天的补番集数改成指定值，并同步总进度。

    Args:
        entry: 补番条目。
        count: 这一天要记下的集数。
        today: 日期，默认今天。
    Returns:
        (是否变更, 说明文案)。
    """
    day = today or _today_date_str()
    entry = normalize_backlog_entry(entry, day)
    title = entry.get("title") or "补番"
    old = today_count(entry, day)
    room = entry["max_ep"] - (entry["watched_ep"] - old)
    target = max(0, min(int(count), room))
    if target == old:
        if int(count) > old:
            return False, f"「{title}」已看到总集数，无法继续增加"
        return False, f"「{title}」今天已是 {old} 集"
    entry["watched_ep"] = entry["watched_ep"] - old + target
    logs = [item for item in entry["logs"] if item["date"] != day]
    if target > 0:
        logs.append({"date": day, "count": target})
    entry["logs"] = logs
    label = progress_label(entry, day)
    if target == 0:
        note = f"「{title}」已撤回今天的补番，{label}"
    else:
        note = f"「{title}」今天补了 {target} 集，{label}"
    if int(count) > target:
        note += f"（受总集数限制，未记到 {int(count)} 集）"
    if is_finished(entry):
        note += "，已完结"
    return True, note


def add_today(entry: dict, delta: int = 1, today: str | None = None) -> tuple[bool, str]:
    """在今天已有集数上再增加 delta 集。"""
    day = today or _today_date_str()
    current = today_count(entry, day)
    return set_today_count(entry, current + max(0, int(delta)), day)


def cycle_today_count(entry: dict, today: str | None = None) -> tuple[bool, str]:
    """连点表情时，把今天的集数按 1→2→3→1 切换。"""
    day = today or _today_date_str()
    current = today_count(entry, day)
    if current == 1:
        nxt = 2
    elif current == 2:
        nxt = 3
    else:
        nxt = 1
    return set_today_count(entry, nxt, day)


def undo_today(entry: dict, today: str | None = None) -> tuple[bool, str]:
    """撤回今天最近的 1 集。"""
    day = today or _today_date_str()
    current = today_count(entry, day)
    title = (entry or {}).get("title") or "补番"
    if current <= 0:
        return False, f"「{title}」今天还没有补番记录"
    return set_today_count(entry, current - 1, day)


def sink_finished(entries: list) -> list:
    """未完结保持原顺序，已完结移到队列末尾。"""
    normalized = [normalize_backlog_entry(e) for e in entries if isinstance(e, dict)]
    active = [e for e in normalized if not is_finished(e)]
    done = [e for e in normalized if is_finished(e)]
    return active + done


def pick_suggestions(
    entries: list, airing_count: int, quota: int = DAILY_WATCH_QUOTA
) -> list[tuple[int, dict, int]]:
    """
    选出今天建议展示的补番。

    Args:
        entries: 补番队列。
        airing_count: 今天仍在更新的新番部数。
        quota: 每天观看名额，默认 3。
    Returns:
        (队列编号, 条目, 建议集数)。当天新番已满观看名额时返回空列表。
    """
    normalized = [normalize_backlog_entry(e) for e in entries if isinstance(e, dict)]
    unfinished = [(i, e) for i, e in enumerate(normalized, 1) if not is_finished(e)]
    if not unfinished:
        return []
    gap = max(0, int(quota) - max(0, int(airing_count)))
    if gap <= 0:
        return []
    if len(unfinished) == 1:
        index, entry = unfinished[0]
        return [(index, entry, gap)]
    return [(i, e, 1) for i, e in unfinished[:gap]]


def format_entry_line(index: int, entry: dict, suggested: int | None = None, today: str | None = None) -> str:
    """格式化单行补番进度。"""
    line = f"  {index}. {entry.get('title') or '补番'} · {progress_label(entry, today)}"
    if suggested:
        line += f" · 建议今日 {suggested} 集"
    return line


def format_full_list(entries: list, today: str | None = None) -> str:
    """格式化完整补番队列。"""
    normalized = [normalize_backlog_entry(e, today) for e in entries if isinstance(e, dict)]
    if not normalized:
        return "📚 补番队列是空的。发送「补番上传 名称 [总集数] [已看]」添加，多部可用换行或 | 分隔。"
    lines = [f"📚 补番共 {len(normalized)} 部："]
    for i, entry in enumerate(normalized, 1):
        lines.append(format_entry_line(i, entry, today=today))
    lines.append("更新：补了 编号 / 补了 编号 N；撤回：补番撤回 编号")
    return "\n".join(lines)


def format_today_push(
    entries: list, airing_count: int, today: str | None = None, quota: int = DAILY_WATCH_QUOTA
) -> str | None:
    """
    生成今日番剧的补番文案。

    当天未看满且队列里还有未完结补番时才返回内容。
    """
    gap = max(0, int(quota) - max(0, int(airing_count)))
    if gap <= 0:
        return None
    picks = pick_suggestions(entries, airing_count, quota)
    if not picks:
        return None
    lines = [_suggestion_header(airing_count, gap, picks)]
    for index, entry, suggested in picks:
        show = suggested if gap > 0 else None
        lines.append(format_entry_line(index, entry, show, today))
    lines.append(_suggestion_tip(len(picks)))
    return "\n".join(lines)


def _suggestion_header(airing_count: int, gap: int, picks: list) -> str:
    if gap <= 0:
        return f"📚 今日补番（今天新番已有 {airing_count} 部，名额已满，仍可记录）"
    if len(picks) == 1 and picks[0][2] > 1:
        return f"📚 今日补番（新番 {airing_count} 部，这一部建议再补 {picks[0][2]} 集）"
    return f"📚 今日补番（新番 {airing_count} 部，建议再补 {gap} 部）"


def _suggestion_tip(pick_count: int) -> str:
    if pick_count == 1:
        how = "表情：贴 1/2/3 表示今天看了几集"
    else:
        how = "表情：点编号，连点改为今天 1/2/3 集"
    return f"{how}\n指令：补了 编号 / 补了 编号 N；完整列表发送「补番」"


def parse_upload_block(text: str) -> list[tuple[str, int | None, int | None]]:
    """
    解析一条或多条补番上传。

    每行格式：名称 [总集数] [已看]。多行，或用 | 分隔。
    """
    chunks = [part.strip() for part in re.split(r"[\n|｜]+", text or "") if part.strip()]
    parsed = []
    for chunk in chunks:
        item = _parse_upload_line(chunk)
        if item:
            parsed.append(item)
    return parsed


def _parse_upload_line(line: str) -> tuple[str, int | None, int | None] | None:
    parts = line.split()
    nums: list[int] = []
    while parts and parts[-1].isdigit() and len(nums) < 2 and len(parts) > 1:
        nums.append(int(parts.pop()))
    nums.reverse()
    title = " ".join(parts).strip()
    if not title:
        return None
    max_ep = nums[0] if nums else None
    watched = nums[1] if len(nums) > 1 else None
    return title, max_ep, watched


def apply_emoji(
    entries: list,
    emoji_number: int,
    airing_count: int,
    quota: int = DAILY_WATCH_QUOTA,
    today: str | None = None,
) -> tuple[list, bool, str]:
    """
    按补番推送上的数字表情更新今天的集数。

    只有一条建议时，1/2/3 直接表示今天看了几集。
    有多条时，数字是队列编号，连点在 1、2、3 集之间切换。
    """
    normalized = sink_finished([normalize_backlog_entry(e, today) for e in entries if isinstance(e, dict)])
    picks = pick_suggestions(normalized, airing_count, quota)
    if not picks:
        return normalized, False, "当前没有可记录的补番"
    if len(picks) == 1:
        return _apply_single_emoji(normalized, picks[0], emoji_number, today)
    return _apply_indexed_emoji(normalized, picks, emoji_number, today)


def _apply_single_emoji(entries, pick, emoji_number: int, today: str | None) -> tuple[list, bool, str]:
    if emoji_number not in (1, 2, 3):
        return entries, False, "只有一部补番时，请贴 1、2 或 3，表示今天看了几集"
    _index, entry, _suggested = pick
    ok, msg = set_today_count(entry, emoji_number, today)
    return sink_finished(entries), ok, msg


def _apply_indexed_emoji(entries, picks, emoji_number: int, today: str | None) -> tuple[list, bool, str]:
    matched = [pick for pick in picks if pick[0] == emoji_number]
    if not matched:
        return entries, False, f"这条补番消息里没有编号 {emoji_number}"
    _index, entry, _suggested = matched[0]
    ok, msg = cycle_today_count(entry, today)
    return sink_finished(entries), ok, msg


def undo_emoji(
    entries: list,
    emoji_number: int,
    airing_count: int,
    quota: int = DAILY_WATCH_QUOTA,
    today: str | None = None,
) -> tuple[list, bool, str]:
    """取消表情时，撤回对应补番今天的 1 集。"""
    normalized = sink_finished([normalize_backlog_entry(e, today) for e in entries if isinstance(e, dict)])
    picks = pick_suggestions(normalized, airing_count, quota)
    if not picks:
        return normalized, False, "当前没有可撤回的补番"
    if len(picks) == 1:
        entry = picks[0][1]
    else:
        matched = [pick for pick in picks if pick[0] == emoji_number]
        if not matched:
            return normalized, False, f"这条补番消息里没有编号 {emoji_number}"
        entry = matched[0][1]
    ok, msg = undo_today(entry, today)
    return sink_finished(normalized), ok, msg
