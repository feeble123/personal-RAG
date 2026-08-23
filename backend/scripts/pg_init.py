"""PostgreSQL 库初始化脚本（P2 单元1）。

PG 建库的正确路径：`create_all` 一次建出与当前 ORM 完全一致的完整 schema（方言原生 DDL），
再 `alembic stamp head` 标记已到最新版本——**不跑历史 SQLite 迁移链**（那 3 个手写
SQLite DDL 迁移在 PG 上会炸，已在迁移文件入口加 `assert_sqlite_or_raise` 守卫）。

用法（backend 目录下，先设好 .env 的 DATABASE_URL=postgresql+asyncpg://...）：
    .venv/Scripts/python.exe scripts/pg_init.py

安全护栏：DATABASE_URL 不以 postgresql 开头时直接退出（防止误对 SQLite 库跑 create_all 造成混乱）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 确保可导入 backend/app 包（无论从哪个目录运行）
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("pg_init")


async def _create_all(url: str) -> None:
    """create_all：方言原生 DDL，一次建出与 ORM 完全一致的完整 schema。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import models  # noqa: F401  确保所有模型注册到 Base.metadata
    from app.db.base import Base

    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("create_all 完成：PostgreSQL schema 已建")
    finally:
        await engine.dispose()


def _stamp_head(url: str) -> None:
    """alembic stamp head：标记已到最新版本（在 event loop 外调用，避免嵌套 asyncio.run）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.stamp(cfg, "head")
    logger.info("alembic stamp head 完成：迁移版本标记到最新")


def main() -> None:
    from app.core.config import settings

    url = settings.database_url
    if not url.startswith("postgresql"):
        logger.error(
            "DATABASE_URL 不是 PostgreSQL 连接串（当前: %s）。"
            "请先在 backend/.env 设置 DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db，"
            "再运行本脚本。",
            url,
        )
        sys.exit(1)

    asyncio.run(_create_all(url))
    # 退出 event loop 后再 stamp（alembic async env 会用 asyncio.run，嵌套会冲突）
    _stamp_head(url)

    logger.info("PG 初始化完成。下一步：")
    logger.info("  1) python -m alembic current   # 应显示 head（f9a0b1c2d3e4 或更新）")
    logger.info("  2) 启动应用后 curl /api/health 看 checks.db=ok")
    logger.info("  3) 迁移真实数据：scripts/migrate_sqlite_to_pg.py（P2 单元4）")


if __name__ == "__main__":
    main()
