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
    is_sqlite = settings.is_sqlite

    # MySQL/PostgreSQL 迁移路径：给足连接池（SQLite 也同参，WAL 下多读并发安全）
    engine_kwargs: dict = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
    }
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
    """建表（幂等）。仅用于开发/测试快速建表；生产 schema 变更走 Alembic（`alembic upgrade head`）。

    create_all 只创建缺失的表、不补已有表的列——旧库升级请先 `alembic stamp head`（P0-6）。
    原启动时手写 17 条 ALTER 已移除，职责移交 Alembic（见 alembic/versions/*）。
    """
    from app.db import models  # noqa: F401  确保模型注册
    from app.db.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured.")


async def ensure_db_at_head() -> None:
    """启动时检查数据库迁移版本是否为 head（只检查不自动迁移，P0-6）。

    未纳入 alembic 管理（无 alembic_version 表，如新库/尚未 stamp 的旧库）→ INFO 提示；
    版本落后 → WARNING 提示运行 `alembic upgrade head`。失败静默（不阻塞启动）。

    P2 单元1：改为异步——原 `engine.sync_engine.connect()` 在 asyncpg 驱动下会抛错；
    `conn.run_sync` 使 SQLite 与 PostgreSQL 双通。
    """
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        # session.py 位于 app/db/ → parents[2] 即 backend/，alembic/ 在 backend/ 下
        alembic_dir = Path(__file__).resolve().parents[2] / "alembic"
        cfg = Config()
        cfg.set_main_option("script_location", str(alembic_dir))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()

        async with engine.begin() as conn:
            # sync 的 Alembic 调用统一包进 run_sync（asyncpg 驱动下 sync connect 会抛错）
            def _check(conn_sync) -> tuple[bool, str | None]:
                has_ver = conn_sync.dialect.has_table(conn_sync, "alembic_version")
                if not has_ver:
                    return False, None
                ctx = MigrationContext.configure(conn_sync)
                return True, ctx.get_current_revision()

            has_ver, current = await conn.run_sync(_check)
        if not has_ver:
            logger.info(
                "数据库未纳入 Alembic 迁移管理（新库或尚未 stamp）；"
                "后续 schema 变更请先执行 alembic upgrade head"
            )
            return
        if current != head:
            logger.warning(
                "数据库迁移版本 %s != head %s，请执行 alembic upgrade head", current, head
            )
        else:
            logger.info("数据库迁移版本 = head（%s）", head)
    except Exception:
        logger.warning("数据库迁移版本检查失败（忽略，不阻塞启动）", exc_info=True)
