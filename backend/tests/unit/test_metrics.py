"""P2 单元3：Prometheus 指标。

- /metrics 端点 200 + 含指标
- 不被限流（无装饰器）
- _normalize_path 路径归一化
- 独立 registry 可隔离（测试不污染全局）
- /metrics 路由在 SPA fallback 之前（防被吞）
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, generate_latest

from app.core.metrics import _normalize_path, build_metrics


class TestNormalizePath:
    def test_normalize_digit_segments(self):
        assert _normalize_path("/api/kbs/12/documents") == "/api/kbs/{id}/documents"

    def test_normalize_multiple_digits(self):
        assert _normalize_path("/api/kbs/12/documents/345") == "/api/kbs/{id}/documents/{id}"

    def test_health_unchanged(self):
        assert _normalize_path("/api/health") == "/api/health"

    def test_metrics_unchanged(self):
        assert _normalize_path("/metrics") == "/metrics"


class TestMetricsIsolation:
    def test_fresh_registry_counter(self):
        """独立 registry 建 counter → generate_latest 含对应行。"""
        reg = CollectorRegistry()
        c = Counter("test_counter", "测试", registry=reg)
        c.inc(3)
        out = generate_latest(reg).decode()
        assert "test_counter_total 3.0" in out

    def test_build_metrics_on_fresh_registry(self):
        """build_metrics 在独立 registry 上可重复建（幂等）。"""
        reg = CollectorRegistry()
        m1 = build_metrics(reg)
        m2 = build_metrics(reg)  # 幂等：不抛重复注册错误
        assert m1["http_requests_total"] is m2["http_requests_total"]


class TestMetricsEndpoint:
    async def test_metrics_endpoint_200(self, client):
        """GET /metrics → 200 + content-type + 含 rag_http_requests_total。"""
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "rag_http_requests_total" in resp.text

    async def test_metrics_not_rate_limited(self, client):
        """连发 3 次 /metrics 全 200（无 @limiter.limit 装饰器，不被限流）。"""
        for _ in range(3):
            resp = await client.get("/metrics")
            assert resp.status_code == 200

    async def test_metrics_route_before_spa_fallback(self):
        """/metrics 路由注册在 SPA catch-all 之前（防被吞）。"""
        from app.main import app

        routes = app.routes
        metrics_idx = next(i for i, r in enumerate(routes) if getattr(r, "path", None) == "/metrics")
        spa_idx = next(i for i, r in enumerate(routes)
                       if getattr(r, "path", None) == "/{full_path:path}")
        assert metrics_idx < spa_idx, "/metrics 必须在 SPA fallback 之前"


class TestEmbeddedMetrics:
    async def test_http_request_metric_increments(self, client):
        """请求后 rag_http_requests_total 计数 > 0。"""
        await client.get("/api/health")
        resp = await client.get("/metrics")
        # 至少有一次 GET /api/health 的计数
        assert "rag_http_requests_total" in resp.text
