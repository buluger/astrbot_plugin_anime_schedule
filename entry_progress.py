"""番剧条目进度与停播状态的领域逻辑。"""

from __future__ import annotations

from datetime import datetime

DEFAULT_MAX_EP = 12


def _today_date_str() -> str:
    """返回今天的日期字符串。"""
    return datetime.now().strftime("%Y-%m-%d")


def _normalize_entry(entry: dict) -> dict:
    """
    补齐集数进度与停播字段。

    Args:
        entry: 番剧条目字典。
    Returns:
        原地补齐后的条目；非 dict 则原样返回。
    """
    if not isinstance(entry, dict):
        return entry
    try:
        watched = int(entry.get("watched_ep", 0) or 0)
    except (TypeError, ValueError):
        watched = 0
    try:
        max_ep = int(entry.get("max_ep", DEFAULT_MAX_EP) or DEFAULT_MAX_EP)
    except (TypeError, ValueError):
        max_ep = DEFAULT_MAX_EP
    max_ep = max(1, min(999, max_ep))
    watched = max(0, min(watched, max_ep))
    entry["watched_ep"] = watched
    entry["max_ep"] = max_ep
    if "last_watched_date" not in entry:
        entry["last_watched_date"] = None
    entry["paused"] = _coerce_paused(entry.get("paused", False))
    return entry


def _coerce_paused(value) -> bool:
    """把存储值规范成布尔停播标记。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _is_paused(entry: dict) -> bool:
    """判断条目是否处于停播/暂停。"""
    entry = _normalize_entry(entry)
    return bool(entry.get("paused"))


def _should_watch_ep(entry: dict) -> int:
    """当前应看集数：已看最新集 + 1；已完结则返回 max_ep。"""
    entry = _normalize_entry(entry)
    watched = entry["watched_ep"]
    max_ep = entry["max_ep"]
    if watched >= max_ep:
        return max_ep
    return watched + 1


def _is_finished(entry: dict) -> bool:
    """判断是否已看到总集数。"""
    entry = _normalize_entry(entry)
    return entry["watched_ep"] >= entry["max_ep"]


def _is_watched_today(entry: dict) -> bool:
    """判断今天是否已标记看过。"""
    return (entry.get("last_watched_date") or "") == _today_date_str()


def _progress_label(entry: dict) -> str:
    """生成列表用的进度文案。"""
    entry = _normalize_entry(entry)
    watched = entry["watched_ep"]
    max_ep = entry["max_ep"]
    if _is_paused(entry):
        base = f"已看{watched}集 · ⏸停播/{max_ep}"
    elif _is_finished(entry):
        base = f"已看{watched}集 · 已完结/{max_ep}"
    else:
        base = f"已看{watched}集 · 应看第{_should_watch_ep(entry)}集/{max_ep}"
    if _is_watched_today(entry) and not _is_paused(entry):
        base += " · ✅今日已看"
    return base


def _mark_watched_one(entry: dict) -> tuple[bool, str]:
    """
    标记看过一集。停播条目会被跳过。

    Args:
        entry: 番剧条目。
    Returns:
        (是否变更, 说明文案)。
    """
    entry = _normalize_entry(entry)
    title = entry.get("title") or "番剧"
    if _is_paused(entry):
        return False, f"「{title}」已停播，已跳过"
    if _is_finished(entry):
        return False, f"「{title}」已看完（{entry['watched_ep']}/{entry['max_ep']}）"
    entry["watched_ep"] = entry["watched_ep"] + 1
    entry["last_watched_date"] = _today_date_str()
    if _is_finished(entry):
        return True, f"「{title}」已看第{entry['watched_ep']}集，已完结"
    return True, (
        f"「{title}」已看第{entry['watched_ep']}集，"
        f"下次应看第{_should_watch_ep(entry)}集"
    )


def _undo_watched_one(entry: dict) -> tuple[bool, str]:
    """
    撤回最近一次已看。停播条目会被跳过。

    Args:
        entry: 番剧条目。
    Returns:
        (是否变更, 说明文案)。
    """
    entry = _normalize_entry(entry)
    title = entry.get("title") or "番剧"
    if _is_paused(entry):
        return False, f"「{title}」已停播，已跳过"
    if entry["watched_ep"] <= 0:
        return False, f"「{title}」当前已看 0 集，无法撤回"
    entry["watched_ep"] -= 1
    if _is_watched_today(entry):
        entry["last_watched_date"] = None
    return True, (
        f"「{title}」已撤回到已看{entry['watched_ep']}集，"
        f"应看第{_should_watch_ep(entry)}集"
    )


def _set_watched_one(entry: dict, watched: int) -> tuple[bool, str]:
    """
    直接设置已看集数。停播条目会被跳过。

    Args:
        entry: 番剧条目。
        watched: 目标已看集数。
    Returns:
        (是否变更, 说明文案)。
    Raises:
        ValueError: watched 无法转为整数。
    """
    entry = _normalize_entry(entry)
    title = entry.get("title") or "番剧"
    if _is_paused(entry):
        return False, f"「{title}」已停播，已跳过"
    n = max(0, min(entry["max_ep"], int(watched)))
    entry["watched_ep"] = n
    entry["last_watched_date"] = _today_date_str() if n > 0 else None
    return True, (
        f"「{title}」已看设为 {n} 集，"
        f"应看第{_should_watch_ep(entry)}集/{entry['max_ep']}"
    )


def _pause_one(entry: dict) -> tuple[bool, str]:
    """
    将条目标为停播。

    Args:
        entry: 番剧条目。
    Returns:
        (是否变更, 说明文案)。
    """
    entry = _normalize_entry(entry)
    title = entry.get("title") or "番剧"
    if entry["paused"]:
        return False, f"「{title}」已经是停播状态"
    entry["paused"] = True
    return True, f"「{title}」已暂停，更新已看时会跳过"


def _resume_one(entry: dict) -> tuple[bool, str]:
    """
    恢复停播条目。

    Args:
        entry: 番剧条目。
    Returns:
        (是否变更, 说明文案)。
    """
    entry = _normalize_entry(entry)
    title = entry.get("title") or "番剧"
    if not entry["paused"]:
        return False, f"「{title}」当前未停播"
    entry["paused"] = False
    return True, (
        f"「{title}」已恢复，"
        f"下次应看第{_should_watch_ep(entry)}集/{entry['max_ep']}"
    )
