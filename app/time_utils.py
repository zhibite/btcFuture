"""统一时间工具：显式使用 Asia/Shanghai (UTC+8) 时区，避免依赖容器/系统时区。

所有展示给用户的时间戳（交易记录 / 每日聚合键）必须走这里。
"""
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    BJ_TZ = ZoneInfo("Asia/Shanghai")
except ImportError:  # 兜底：旧版本 / 容器异常时退化到固定 +8 偏移
    from datetime import timezone, timedelta
    BJ_TZ = timezone(timedelta(hours=8))


def now_bj() -> datetime:
    """返回带时区的当前北京时间（aware datetime, Asia/Shanghai）。"""
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