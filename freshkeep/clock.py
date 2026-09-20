"""时间工具：统一使用时区感知的 UTC 时间，ISO-8601 字符串对外。"""

from datetime import datetime, timezone


def parse_iso(value):
    """解析 ISO-8601 字符串；朴素时间按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt):
    """时区感知时间转 ISO-8601（毫秒精度，Z 结尾）。"""
    dt = parse_iso(dt)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_iso():
    return to_iso(datetime.now(timezone.utc))


class SimClock:
    """可注入的时钟，便于测试中按确定的时间线回放整张订单。"""

    def __init__(self, start=None):
        self._now = parse_iso(start) if start else datetime.now(timezone.utc)

    def now(self):
        return self._now

    def now_iso(self):
        return to_iso(self._now)

    def advance(self, **kwargs):
        from datetime import timedelta

        self._now += timedelta(**kwargs)
        return self._now

    def set(self, value):
        self._now = parse_iso(value)
        return self._now
