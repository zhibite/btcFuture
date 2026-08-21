"""统一时间工具：显式使用 Asia/Shanghai (UTC+8) 时区，避免依赖容器/系统时区。

设计原则：用 fixed +8 偏移（timezone(timedelta(hours=8))）而不是 ZoneInfo，
原因：
  1. ZoneInfo 在 Debian slim / alpine 镜像里需要系统 tzdata，否则抛 ZoneInfoNotFoundError
  2. 北京时间相对 UTC 的偏移是恒定的 +8（中国不实行夏令时），不需要查数据库
  3. 行为完全确定，不依赖 /usr/share/zoneinfo 或 Python tzdata 包是否安装

所有展示给用户的时间戳（交易记录 / 每日聚合键 / API timestamp）必须走这里。
"""
from datetime import datetime, timezone, timedelta

# 固定 +8 偏移，不随夏令时变化（中国 1991 年起永久 UTC+8）。
BJ_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_bj() -> datetime:
    """返回带时区的当前北京时间（aware datetime, +08:00）。"""
    return datetime.now(BJ_TZ)


def format_ts(dt: datetime = None) -> str:
    """'YYYY-MM-DD HH:MM:SS' 北京时间。与旧 time.strftime 行为等价，前端无需改动。"""
    if dt is None:
        dt = now_bj()
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def format_date(dt: datetime = None) -> str:
    """'YYYY-MM-DD' 北京时间。用于每日聚合键。"""
    if dt is None:
        dt = now_bj()
    return dt.strftime('%Y-%m-%d')


def iso_bj(dt: datetime = None) -> str:
    """ISO 字符串（含 +08:00 偏移）。DB 持久化用，便于跨时区追溯。"""
    if dt is None:
        dt = now_bj()
    return dt.isoformat()
