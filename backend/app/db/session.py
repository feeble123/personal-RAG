"""异步数据库会话：engine + 连接池 + SQLite WAL/PRAGMA。

升级路径：将 `DATABASE_URL` 改为 MySQL/PostgreSQL 连接串即可迁移，
SQLite 专用 PRAGMA 通过方言判断守卫，不阻塞迁移。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

logger = logging.getLogger(__name__)


def _ensure_data_dir(url: str) -> None:
    """SQLite：引擎创建前确保数据库文件所在目录存在（不依赖 lifespan）。"""
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def _make_engine():
    url = settings.database_url
    _ensure_data_dir(url)
    is_sqlite = url.startswith("sqlite")

    engine_kwargs: dict = {}
    if is_sqlite:
        # SQLite 连接极轻，池的意义主要是规避并发写锁；WAL 下多读并发安全。
        engine_kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )
    else:
        # MySQL/PostgreSQL 迁移路径：给足连接池
        engine_kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )

    engine = create_async_engine(url, echo=settings.debug, **engine_kwargs)

    if is_sqlite:
        # aiosqlite 需通过 sync_engine 的 connect 事件设置 PRAGMA
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


engine = _make_engine()
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个会话，请求结束关闭。"""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """建表（幂等）。"""
    from app.db import models  # noqa: F401  确保模型注册
    from app.db.base import Base

    # SQLite 使用 NullPool 不适合；此处 engine 已有池。建表即可。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 轻量迁移：语义缓存新增 subject 列（create_all 不补已有表的列）
        from sqlalchemy import text

        cols = await conn.run_sync(
            lambda sync_conn: [
                row[1]
                for row in sync_conn.execute(text("PRAGMA table_info(semantic_cache)")).fetchall()
            ]
        )
        if cols and "subject" not in cols:
            await conn.execute(text("ALTER TABLE semantic_cache ADD COLUMN subject VARCHAR(200)"))
            logger.info("migration: semantic_cache.subject added")
        # BUG-B：缓存按检索作用域（kb/doc）隔离，需补 kb_id / doc_scope 列
        if cols and "kb_id" not in cols:
            await conn.execute(text("ALTER TABLE semantic_cache ADD COLUMN kb_id INTEGER"))
            logger.info("migration: semantic_cache.kb_id added")
        if cols and "doc_scope" not in cols:
            await conn.execute(text("ALTER TABLE semantic_cache ADD COLUMN doc_scope VARCHAR(100)"))
            logger.info("migration: semantic_cache.doc_scope added")
    logger.info("Database tables ensured.")
