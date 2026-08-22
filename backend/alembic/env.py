"""Alembic 迁移环境（async SQLAlchemy + SQLite）。

- URL 从 app settings 读取，可用环境变量 ALEMBIC_DATABASE_URL 覆盖（测试独立临时库）
- render_as_batch=True：SQLite 改列/删约束走 batch 模式（换表重建）
- SQLite 迁移连接关闭 foreign_keys（batch 换表需要）；应用连接仍由 session.py:58 开启
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import event, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.base import Base

import app.db.models  # noqa: F401  确保模型注册进 metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL：测试可经 ALEMBIC_DATABASE_URL 覆盖（独立临时库），默认用应用配置
_db_url = os.environ.get("ALEMBIC_DATABASE_URL") or settings.database_url
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if _db_url.startswith("sqlite"):
        # SQLite 批处理换表需临时关外键（应用连接由 session.py:58 开启 foreign_keys=ON）
        @event.listens_for(connectable.sync_engine, "connect")
        def _sqlite_fk_off(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
