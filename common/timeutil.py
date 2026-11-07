# -*- coding: utf-8 -*-
"""时间工具：统一固定 +08:00 偏移。

本机 TZPATH 为空且未安装 tzdata，`zoneinfo.ZoneInfo("Asia/Shanghai")` 会抛
ZoneInfoNotFoundError。因此全程不使用 zoneinfo，也不使用已废弃的
`datetime.utcnow()`。

对外一律传 ISO 8601 字符串（UTC 带 +00:00 后缀），只在显示时转本地时间。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
CST = timezone(timedelta(hours=8), name="CST")  # 中国标准时间，固定偏移，不依赖 tzdata

MS = 1000


def now_utc() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


def now_utc_iso() -> str:
    """当前 UTC 时间，ISO 8601 毫秒精度，例如 2026-10-05T08:30:12.345+00:00。"""
    return now_utc().isoformat(timespec="milliseconds")


def now_epoch() -> float:
    """当前 Unix 时间戳（秒，浮点）。"""
    return now_utc().timestamp()


def epoch_ms(dt: datetime | None = None) -> int:
    """Unix 时间戳（毫秒，整数）。默认取当前时间。"""
    d = dt or now_utc()
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.timestamp() * MS)


def parse_iso(s: str) -> datetime:
    """解析 ISO 8601 字符串。无法解析时抛 ValueError。"""
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def to_datetime(ts: str | float | int | datetime) -> datetime:
    """把 ISO 字符串 / epoch 秒 / epoch 毫秒 / datetime 统一成带时区的 datetime。

    启发式：数值大于 1e11 视为毫秒，否则视为秒。
    """
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        sec = ts / MS if abs(ts) > 1e11 else float(ts)
        return datetime.fromtimestamp(sec, UTC)
    return parse_iso(str(ts))


def fmt_local(ts: str | float | int | datetime, with_ms: bool = False) -> str:
    """转成本地时间字符串，例如 2026-10-05 16:30:12。"""
    d = to_datetime(ts).astimezone(CST)
    return d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if with_ms else d.strftime("%Y-%m-%d %H:%M:%S")


def fmt_local_short(ts: str | float | int | datetime) -> str:
    """本地时间短格式，例如 10-05 16:30。"""
    return to_datetime(ts).astimezone(CST).strftime("%m-%d %H:%M")


def fmt_clock(ts: str | float | int | datetime) -> str:
    """本地时间只保留时分秒，例如 16:30:12。"""
    return to_datetime(ts).astimezone(CST).strftime("%H:%M:%S")


def now_local_str() -> str:
    """当前本地时间字符串。"""
    return fmt_local(now_utc())


def start_of_local_day(ts: str | float | int | datetime | None = None) -> float:
    """本地时间"今天零点"的 Unix 时间戳（秒）。

    注意不是 UTC 零点 —— 界面上说的"今天"是现场的那一天。
    """
    d = to_datetime(ts if ts is not None else now_utc()).astimezone(CST)
    midnight = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def local_day(ts: str | float | int | datetime) -> str:
    """本地日期 'YYYY-MM-DD'。"""
    return to_datetime(ts).astimezone(CST).strftime("%Y-%m-%d")


def local_hour_floor(ts: str | float | int | datetime | None = None) -> float:
    """本地时间"本小时整点"的 Unix 时间戳（秒）。

    CSV 按本地小时轮转（见 terminal/store.py 的 CsvLog.hour_key），
    凡是"要造一批落在同一个文件里的记录"的地方都得从这里起跳 ——
    直接用"现在"的话，在每小时的 58 分造数据就会跨进下一个小时，
    被拆成两个文件。
    """
    d = to_datetime(ts if ts is not None else now_utc()).astimezone(CST)
    return d.replace(minute=0, second=0, microsecond=0).timestamp()


def humanize_age(ts: str | float | int | datetime) -> str:
    """把时间描述成"3 秒前"这样的相对形式，用于心跳显示。"""
    delta = (now_utc() - to_datetime(ts)).total_seconds()
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"
