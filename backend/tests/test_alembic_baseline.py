"""P0-6 Alembic 基线奇偶校验：baseline migration 建出的 schema 与 ORM metadata 完全一致。

用独立临时库跑 `alembic upgrade head`，再与 `Base.metadata` 用 compare_metadata 对比；
任何表/列/约束/索引差异都视为 baseline 不准确（验收：新库 schema 与 ORM 一致）。
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from app.db.base import Base

import app.db.models  # noqa: F401  确保模型注册进 metadata

BASE_DIR = Path(__file__).resolve().parents[1]


def test_baseline_schema_matches_metadata(tmp_path, monkeypatch):
    """空库 alembic upgrade head 建出的 schema 与 ORM 完全一致（无 diff）。"""
    db_file = tmp_path / "alembic.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert not diff, f"baseline 与 ORM 不一致:\n{diff}"


def test_upgrade_downgrade_roundtrip(tmp_path, monkeypatch):
    """head → downgrade baseline 前 → upgrade head 往返可执行（迁移可回滚）。"""
    db_file = tmp_path / "alembic2.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert not diff, f"往返后 schema 与 ORM 不一致:\n{diff}"
