"""发布时间控制：活跃时段、星期、每日配额。

工作流是持续反复运行的；这里决定"某一轮到底允不允许搬运"。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

_HHMM = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")

WEEKDAY_LABELS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}

DAILY_KEY = "daily_quota"


@dataclass
class Gate:
    """一次时间检查的结论。"""

    allowed: bool
    reason: str = ""
    remaining: int | None = None  # 今日还能发多少条；None 表示不限


def parse_hhmm(s: str) -> dt.time | None:
    m = _HHMM.match(s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return dt.time(h, mi)


def parse_window(spec: str) -> tuple[dt.time, dt.time] | None:
    """解析 "09:00-23:00"；返回 None 表示不限制。跨夜写法 "22:00-06:00" 也支持。"""
    spec = (spec or "").strip()
    if not spec:
        return None
    parts = re.split(r"\s*[-—~至]\s*", spec)
    if len(parts) != 2:
        raise ValueError(f"时段格式应为 09:00-23:00，收到 {spec!r}")
    start, end = parse_hhmm(parts[0]), parse_hhmm(parts[1])
    if start is None or end is None:
        raise ValueError(f"时段格式应为 09:00-23:00，收到 {spec!r}")
    return start, end


def in_window(now: dt.time, window: tuple[dt.time, dt.time]) -> bool:
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end  # 跨夜


def describe_window(spec: str) -> str:
    try:
        w = parse_window(spec)
    except ValueError:
        return spec
    if w is None:
        return "全天"
    start, end = w
    cross = "（跨夜）" if start > end else ""
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}{cross}"


def describe_days(days: list[int] | None) -> str:
    if not days:
        return "每天"
    return "周" + "、".join(WEEKDAY_LABELS.get(int(d), str(d)) for d in sorted(days))


def now_in(timezone: str = "") -> dt.datetime:
    """取当前时间。timezone 为空用服务器本地时区。"""
    if timezone:
        try:
            from zoneinfo import ZoneInfo

            return dt.datetime.now(ZoneInfo(timezone))
        except Exception:  # 时区名无效就退回本地
            pass
    return dt.datetime.now()


def check_window(workflow: Any, now: dt.datetime | None = None) -> Gate:
    """只检查时段和星期，不含配额（配额要读库，见 check_quota）。"""
    now = now or now_in(getattr(workflow, "timezone", "") or "")

    days = list(getattr(workflow, "active_days", None) or [])
    if days and now.isoweekday() not in [int(d) for d in days]:
        return Gate(False, f"今天（周{WEEKDAY_LABELS[now.isoweekday()]}）不在运行日 {describe_days(days)} 内")

    spec = getattr(workflow, "active_hours", "") or ""
    if spec:
        try:
            window = parse_window(spec)
        except ValueError as e:
            return Gate(False, str(e))
        if window and not in_window(now.time(), window):
            return Gate(False, f"当前 {now.strftime('%H:%M')} 不在活跃时段 {describe_window(spec)} 内")

    return Gate(True)


def today_key(timezone: str = "") -> str:
    return now_in(timezone).strftime("%Y-%m-%d")


def quota_state(raw: Any, timezone: str = "") -> dict[str, Any]:
    """把存储里的配额记录规整化，跨天自动归零。"""
    today = today_key(timezone)
    if not isinstance(raw, dict) or raw.get("date") != today:
        return {"date": today, "count": 0}
    return {"date": today, "count": int(raw.get("count") or 0)}


def check_quota(limit: int, state: dict[str, Any]) -> Gate:
    if not limit or limit <= 0:
        return Gate(True)
    used = int(state.get("count") or 0)
    left = limit - used
    if left <= 0:
        return Gate(False, f"今日已达发布上限 {limit} 条", remaining=0)
    return Gate(True, remaining=left)


def utc_iso(t: dt.datetime | None) -> str | None:
    """把时间统一成不带时区的 UTC 字符串。库里的时间戳一律是这个格式。

    APScheduler 给的是带本地时区的时间，直接 isoformat() 会带上偏移；前端只在
    字符串以 Z 结尾或含 '+' 时才认时区，负偏移（UTC 以西）的串两者都不满足，
    会被补成 "…-05:00Z" 这种非法值，new Date() 直接给 Invalid Date。
    """
    if t is None:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t.isoformat()
