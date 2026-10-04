from datetime import UTC, datetime


def utc_now() -> datetime:
    """获取按世界标准时间计时、不携带时区标记的当前时间。

    数据库现有时间列使用无时区格式，因此先取 UTC（世界标准时间），再去掉标记；不是转换成本地时间。
    """

    return datetime.now(UTC).replace(tzinfo=None)


def utc_isoformat(value: datetime) -> str:
    """将数据库的无时区 UTC 时间或带时区时间输出为明确的 UTC 文本。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
