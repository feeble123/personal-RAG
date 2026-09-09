"""统一时间工具（G5-2）。

系统统一使用 UTC tz-aware datetime：
- 应用代码落库统一调 `utcnow()`（带时区，不再 `.replace(tzinfo=None)` 剥时区）
- 数据库列统一 `DateTime(timezone=True)`（PG 下 timestamptz）
- `server_default=func.now()` 返回数据库会话时区的当前时间，迁移时统一转 UTC

历史背景：此前多个模块各自写 `datetime.now(timezone.utc).replace(tzinfo=None)`，
既重复又让「数据库 now() 生成的列（本地时区）」与「Python 写的列（UTC）」基准不一致。
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（tz-aware）。落库/比较/写时间戳统一入口。"""
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """把 DB 读回的时间归一化为 tz-aware UTC。

    SQLite 的 DATETIME 不存时区：存 aware、读回 naive（数值正确 = UTC 墙钟）。
    PG 的 timestamptz 读回已带 tzinfo。两者统一成 aware UTC，供相减/比较，
    避免「naive - aware」的 TypeError。None 原样返回。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utcnow_naive() -> datetime:
    """naive UTC 墙钟（供 SQL 比较边界值用）。

    SQLite 存 naive UTC 墙钟（读回无 tzinfo）；SQL 层的 `<`/`>` 比较须传 naive，
    否则 aware 值序列化带 `+00:00` 与库内 naive 字符串比较会错位。
    PG 的 timestamptz 列下 SQLAlchemy 按 UTC 解释 naive 值，行为同样正确。
    """
    return utcnow().replace(tzinfo=None)
