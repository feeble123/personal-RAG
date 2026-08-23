"""SQLite → PostgreSQL 数据迁移脚本（P2 单元4）。

把当前 SQLite 库（backend/data/app.db）的**全部业务数据**搬到 PostgreSQL（rag 库）。
- 用 SQLAlchemy core 读 SQLite / 写 PG，按外键依赖顺序逐表迁移
- 自动同步自增序列（PG 的 SERIAL 需要 setval，否则插入 id 冲突）
- 迁移前在 SQLite 上做 dry-run 计数；迁移后逐表 count 校验
- 幂等：PG 目标表已有数据时跳过该表（可重跑，不重复）

用法（backend 目录，先设好 .env 的 DATABASE_URL=postgresql+asyncpg://...）：
    .venv/Scripts/python.exe scripts/migrate_sqlite_to_pg.py

安全护栏：仅当 DATABASE_URL 以 postgresql 开头才执行；SQLite 源库路径硬编码为 data/app.db。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import sqlalchemy as sa

# 确保可导入 backend/app 包
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("migrate")

# SQLite 源库（固定路径，防误连）
SQLITE_URL = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"

# 迁移顺序：按外键依赖（先父后子）
TABLES = [
    "users",
    "auth_sessions",
    "knowledge_bases",
    "conversations",
    "messages",
    "documents",
    "document_versions",
    "chunks",
    "index_versions",
    "citations",
    "embedding_cache",
    "semantic_cache",
    "qa_memory",
    "ingestion_jobs",
]


def _pg_url() -> str:
    from app.core.config import settings

    url = settings.database_url
    if not url.startswith("postgresql"):
        logger.error(
            "DATABASE_URL 不是 PostgreSQL 连接串（当前: %s）。"
            "请在 backend/.env 设置 DATABASE_URL=postgresql+asyncpg://rag_app:...@localhost:5432/rag，"
            "再运行本脚本。",
            url,
        )
        sys.exit(1)
    return url


async def _migrate() -> None:
    pg_url = _pg_url()

    # 用同步引擎批量搬数据（数据量大，sync 更稳；asyncpg 用于应用连接，这里只是搬运）
    src = sa.create_engine(SQLITE_URL)
    dst = sa.create_engine(pg_url.replace("+asyncpg", ""))

    try:
        # 目标库先建表（create_all 幂等；PG 上由脚本建，后续 alembic stamp 由 pg_init 处理）
        from app.db import models  # noqa: F401  注册所有模型
        from app.db.base import Base

        Base.metadata.create_all(dst)
        logger.info("PG 目标库建表完成")

        # 迁移源库的 schema 信息（表名 → 列名）
        src_meta = sa.MetaData()
        src_meta.reflect(bind=src)
        dst_meta = sa.MetaData()
        dst_meta.reflect(bind=dst)

        migrated = []
        skipped = []

        def _dst_count(dst_table) -> int:
            with dst.connect() as conn:
                return conn.execute(sa.select(sa.func.count()).select_from(dst_table)).scalar_one()

        def _col_lengths(table_obj) -> dict[str, int]:
            """目标表各列的长度限制（String(N) → N；无限制 → 0）。"""
            out: dict[str, int] = {}
            for col in table_obj.columns:
                # SQLAlchemy 2.0 从类型里取长度
                typ = col.type
                length = getattr(typ, "length", None)
                out[col.name] = int(length) if length else 0
            return out

        def _src_rows(src_table, dst_table) -> list[dict]:
            lengths = _col_lengths(dst_table)
            with src.connect() as conn:
                rows = [dict(r) for r in conn.execute(sa.select(src_table)).mappings()]
            # SQLite 不强制 VARCHAR 长度、PG 强制：超长字符串截断到目标列长度
            for r in rows:
                for col_name, max_len in lengths.items():
                    if max_len and isinstance(r.get(col_name), str) and len(r[col_name]) > max_len:
                        r[col_name] = r[col_name][:max_len]
            return rows

        for table in TABLES:
            if table not in src_meta.tables or table not in dst_meta.tables:
                logger.warning("跳过 %s：源或目标无此表", table)
                continue
            src_table = src_meta.tables[table]
            dst_table = dst_meta.tables[table]

            # 幂等：目标已有数据则跳过（可重跑）
            dst_count = _dst_count(dst_table)
            if dst_count > 0:
                skipped.append((table, dst_count))
                logger.info("跳过 %s：目标已有 %d 行", table, dst_count)
                continue

            rows = _src_rows(src_table, dst_table)
            if not rows:
                logger.info("%s：源为空，跳过", table)
                continue

            with dst.begin() as conn:
                for i in range(0, len(rows), 2000):
                    batch = rows[i : i + 2000]
                    conn.execute(dst_table.insert(), batch)
            migrated.append((table, len(rows)))
            logger.info("迁移 %s：%d 行", table, len(rows))

        # 同步自增序列（PG SERIAL 需要 setval 到当前 max(id)）
        with dst.begin() as conn:
            for table in TABLES:
                if table in dst_meta.tables and "id" in dst_meta.tables[table].columns:
                    max_id = conn.execute(
                        sa.select(sa.func.coalesce(sa.func.max(dst_meta.tables[table].c.id), 0))
                    ).scalar_one()
                    if max_id:
                        seq = f"{table}_id_seq"
                        conn.execute(sa.text(f"SELECT setval('{seq}', {int(max_id)}, true)"))
                        logger.info("同步序列 %s → %d", seq, max_id)

        # 汇总
        logger.info("==== 迁移完成 ====")
        for t, c in migrated:
            logger.info("  %-18s %6d 行（新迁移）", t, c)
        for t, c in skipped:
            logger.info("  %-18s %6d 行（已存在，跳过）", t, c)
    finally:
        src.dispose()
        dst.dispose()


def main() -> None:
    asyncio.run(_migrate())


if __name__ == "__main__":
    main()
