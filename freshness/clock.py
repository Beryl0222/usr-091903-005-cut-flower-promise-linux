"""时间工具。

领域内部统一使用"感知本地时间"的 ISO 8601 字符串（分钟精度），
由可替换的时钟提供，方便测试回放。
"""

from datetime import datetime, timedelta, timezone


def now(clock=None):
    return format_ts(clock.now() if clock is not None else datetime.now())


def format_ts(dt):
    """datetime -> ISO 字符串。naive 视为本地时间。"""
    return dt.replace(microsecond=0).isoformat()


def parse_ts(value):
    """ISO 字符串 -> aware datetime（按本地时区解释 naive 值）。"""
    if value is None:
        raise ValueError("时间不能为空")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def add_minutes(value, minutes):
    return format_ts(parse_ts(value) + timedelta(minutes=minutes))


def diff_minutes(later, earlier):
    return int((parse_ts(later) - parse_ts(earlier)).total_seconds() // 60)


def minutes_between(start, end):
    """非负的分钟差。"""
    return max(0, diff_minutes(end, start))


def is_within(value, start, end):
    return start <= value <= end


class FixedClock:
    """测试/回放用固定时钟，可手工推进。"""

    def __init__(self, start="2026-09-01T08:00:00"):
        self._now = parse_ts(start)

    def now(self):
        return self._now

    def set(self, value):
        self._now = parse_ts(value)
        return self

    def advance(self, minutes=0, **kwargs):
        kwargs["minutes"] = kwargs.get("minutes", 0) + minutes
        self._now = self._now + timedelta(**kwargs)
        return format_ts(self._now)


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
