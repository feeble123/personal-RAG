"""PostgreSQL 迁移守卫（P2 单元1）。

历史 Alembic 迁移里有 3 个手写 SQLite 专用 DDL（含 `INTEGER NOT NULL PRIMARY KEY`、
`DATETIME`、`ALTER TABLE ... RENAME`），在 PostgreSQL 上直接 `upgrade head` 会炸。

PG 建库的正确路径是 `scripts/pg_init.py`（create_all + alembic stamp head），
历史 SQLite 迁移永不触碰 PG。本模块在迁移文件入口加守卫：
- SQLite 方言：直接放行（历史行为零变化）
- 其他方言（postgresql/mysql）：响亮报错，指向正确路径，避免生成错误 schema 或语法崩溃
"""
from __future__ import annotations


def assert_sqlite_or_raise(dialect_name: str) -> None:
    """校验当前方言为 SQLite，否则抛错并引导到 pg_init.py。

    dialect_name 取 `op.get_bind().dialect.name`（sqlite / postgresql / mysql ...）。
    """
    if dialect_name != "sqlite":
        raise RuntimeError(
            f"本迁移含 SQLite 专用 DDL，不支持方言 {dialect_name!r}。"
            "PostgreSQL/MySQL 建库请用 scripts/pg_init.py（create_all + alembic stamp head），"
            "不要直接 upgrade head 历史 SQLite 迁移。详见 docs/P2-PG-MIGRATION.md"
        )
