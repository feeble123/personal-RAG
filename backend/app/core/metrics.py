"""Prometheus 指标（P2 单元3）。

暴露 `/metrics` 端点输出机器可读数字，供 Grafana 拉取画曲线。
- 指标默认注册到全局 `prometheus_client.REGISTRY`；暴露 `metrics_registry` 参数供测试隔离。
- 热路径埋点只做 dict 自增（纳秒级），不影响业务逻辑。

指标清单：
- rag_http_requests_total / rag_http_request_duration_seconds  ← 请求中间件
- rag_chat_requests_total / rag_chat_duration_seconds          ← qa/routes.py chat
- rag_ingestion_jobs_total / rag_ingestion_jobs_active         ← ingestion/manager.py
- rag_embedding_requests_total                                 ← services/embedding.py
- rag_rerank_requests_total                                    ← services/rag.py
- rag_retrieval_requests_total                                 ← services/rag.py
- rag_semantic_cache_requests_total                            ← services/semantic_cache.py
"""
from __future__ import annotations

import re
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, REGISTRY, generate_latest


def _build_registry(registry: CollectorRegistry | None = None) -> CollectorRegistry:
    """默认全局 REGISTRY，测试可传独立 registry 隔离。"""
    return registry if registry is not None else REGISTRY


def build_metrics(registry: CollectorRegistry | None = None) -> dict[str, Any]:
    """创建全部指标对象（幂等：同一 registry 重复调用返回同一组）。"""
    reg = _build_registry(registry)
    get = _get_or_create

    return {
        # 请求层
        "http_requests_total": get(Counter, "rag_http_requests_total",
                                   "HTTP 请求总数", ["method", "path", "status"], reg),
        "http_request_duration_seconds": get(Histogram, "rag_http_request_duration_seconds",
                                             "HTTP 请求耗时（秒）", ["method", "path"], reg),
        # 问答
        "chat_requests_total": get(Counter, "rag_chat_requests_total",
                                   "问答请求数", ["result"], reg),
        "chat_duration_seconds": get(Histogram, "rag_chat_duration_seconds",
                                     "问答耗时（秒）", [], reg),
        # 入库
        "ingestion_jobs_total": get(Counter, "rag_ingestion_jobs_total",
                                    "入库任务数", ["stage"], reg),
        "ingestion_jobs_active": get(Gauge, "rag_ingestion_jobs_active",
                                     "当前活跃入库任务数", [], reg),
        # 检索链路
        "embedding_requests_total": get(Counter, "rag_embedding_requests_total",
                                        "embedding 调用数", [], reg),
        "rerank_requests_total": get(Counter, "rag_rerank_requests_total",
                                     "rerank 调用数", ["status"], reg),
        "retrieval_requests_total": get(Counter, "rag_retrieval_requests_total",
                                        "检索请求数", ["scope"], reg),
        "semantic_cache_requests_total": get(Counter, "rag_semantic_cache_requests_total",
                                             "语义缓存查询数", ["result"], reg),
    }


def _get_or_create(cls, name: str, doc: str, labelnames: list[str], registry: CollectorRegistry):
    """幂等创建指标：已有则复用，避免重复注册报错。

    兼容不同 prometheus-client 版本的内部存储（_names_to_collectors / _collectors）。
    """
    existing = getattr(registry, "_names_to_collectors", None)
    if existing is not None and name in existing:
        return existing[name]
    for c in getattr(registry, "_collectors", []):  # pragma: no cover - 旧版本兜底
        if getattr(c, "_name", None) == name:
            return c
    return cls(name, doc, labelnames, registry=registry)


# 模块级默认指标（应用启动即就绪）
metrics = build_metrics()

# 路径归一化：数字段 → {id}（压死高基数，如 /api/kbs/12 → /api/kbs/{id}）
_PATH_SEG = re.compile(r"/\d+")


def _normalize_path(path: str) -> str:
    return _PATH_SEG.sub("/{id}", path)


def generate_metrics_text(registry: CollectorRegistry | None = None) -> bytes:
    """生成 /metrics 响应体。"""
    return generate_latest(_build_registry(registry))


def refresh_active_jobs(count: int) -> None:
    """刷新 rag_ingestion_jobs_active（/metrics 拉取时调用，DB 挂则沿用旧值）。"""
    metrics["ingestion_jobs_active"].set(count)
