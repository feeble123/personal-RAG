"""P2 单元1：PG 迁移应用层就绪——守卫 + pg_init 引导脚本 + ensure_db_at_head 异步化。

离线测试（不真连 PG）：
- assert_sqlite_or_raise：SQLite 放行 / 其他方言报错并指向 pg_init
- pg_init 脚本对非 PG 连接串拒绝执行（conftest 已把 DATABASE_URL 设为 sqlite 临时库）
- ensure_db_at_head 异步化后 SQLite 上可正常 await
- @pytest.mark.pg 集成通道留好（RUN_PG_TESTS=1 才跑，默认跳过）
"""
from __future__ import annotations

import os

import pytest

from app.db.pg_guard import assert_sqlite_or_raise


class TestPgGuard:
    def test_pg_guard_passes_sqlite(self):
        """SQLite 方言直接放行（历史迁移在 SQLite 上零变化）。"""
        assert_sqlite_or_raise("sqlite")  # 不抛

    def test_pg_guard_raises_with_guidance(self):
        """非 SQLite 方言（postgresql）→ RuntimeError，消息指向 pg_init 与文档。"""
        with pytest.raises(RuntimeError, match="pg_init"):
            assert_sqlite_or_raise("postgresql")

    def test_pg_guard_raises_mysql_too(self):
        """MySQL 同样被拒（历史迁移同样含 SQLite 专用 DDL）。"""
        with pytest.raises(RuntimeError):
            assert_sqlite_or_raise("mysql")


class TestPgInitScript:
    def test_pg_init_rejects_non_pg_url(self, caplog):
        """连接串不是 postgresql → 拒绝执行（防误跑对 SQLite 库 create_all）。

        conftest 已把 DATABASE_URL 设为 sqlite 临时库，main() 应在建引擎前退出。
        """
        import scripts.pg_init as pg_init

        with pytest.raises(SystemExit) as exc:
            pg_init.main()
        assert exc.value.code == 1
        assert any("不是 PostgreSQL 连接串" in r.getMessage() for r in caplog.records)


class TestEnsureDbAtHeadAsync:
    async def test_ensure_db_at_head_async_ok(self):
        """异步化后 SQLite 上 await 不抛错（lifespan 调用路径）。"""
        from app.db.session import ensure_db_at_head

        # 测试库无 alembic_version 表（create_all 建表）→ INFO 提示，不抛
        await ensure_db_at_head()


class TestPgIntegrationChannel:
    @pytest.mark.pg
    @pytest.mark.skipif(os.environ.get("RUN_PG_TESTS") != "1",
                        reason="PG 集成测试需 RUN_PG_TESTS=1 + 已初始化 PG（见 docs/P2-PG-MIGRATION.md）")
    async def test_pg_full_bootstrap(self):
        """（离线跳过）PG 集成通道：create_all + stamp head 后能连上。"""
        raise AssertionError("RUN_PG_TESTS=1 且 PG 就绪时本测试应被替换为真实 PG 校验")
