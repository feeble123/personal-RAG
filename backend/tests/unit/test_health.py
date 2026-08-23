"""P2 单元2：可观测性·基础——/api/health 真探活。

- DB 正常 → 200 + status=ok + checks.db=ok + 保留 app 字段
- DB 故障 → 503 + status=degraded
"""
from __future__ import annotations


class TestHealth:
    async def test_health_ok(self, client):
        """DB 正常：200 + status=ok + checks.db=ok + app 字段保留（纯增量）。"""
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["checks"]["db"] == "ok"
        assert data["app"]  # 保留原字段

    async def test_health_db_down_503(self, client, monkeypatch):
        """DB 故障：503 + status=degraded + checks.db 报错。"""
        import app.main as app_main

        class _BrokenSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, *a, **k):
                raise RuntimeError("db down")

        # main.py 的 health 用 `async_session_factory` 名字（从 app.db.session import 的值）
        monkeypatch.setattr(app_main, "async_session_factory", lambda: _BrokenSession())
        resp = await client.get("/api/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert "error" in data["checks"]["db"]
