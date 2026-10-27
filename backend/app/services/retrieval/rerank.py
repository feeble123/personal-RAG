"""bge-reranker 重排客户端（G6-2 从 rag.py 抽出）。

G4：模块级 AsyncClient 单例复用连接池（懒加载）。每次问答最多 3 次 rerank，
若每次新建客户端就要重复 3 次 TCP+TLS 握手（数百 ms）。
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_rerank_client = None  # type: ignore[var-annotated]


async def rerank(query: str, documents: list[str]) -> list[float]:
    """硅基流动 bge-reranker API 重排，返回与 documents 对齐的相关性分数（0~1）。

    连接池：keepalive 复用 TCP 连接；单进程内并发安全（httpx 内部调度），
    多 worker 各自持有一个实例。
    """
    global _rerank_client
    import httpx

    if _rerank_client is None:
        _rerank_client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    payload = {
        "model": settings.rerank_model,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
        "return_documents": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.embedding_api_key}",
        "Content-Type": "application/json",
    }
    resp = await _rerank_client.post(
        f"{settings.embedding_base_url.rstrip('/')}/rerank", json=payload, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    scores = [0.0] * len(documents)
    for item in data.get("results", []):
        idx = item.get("index")
        if idx is not None and 0 <= idx < len(scores):
            scores[idx] = item.get("relevance_score", 0.0)
    return scores


async def close_rerank_client() -> None:
    """关闭 rerank 连接池（应用优雅退出时调用；测试可调用重置）。"""
    global _rerank_client
    if _rerank_client is not None:
        await _rerank_client.aclose()
        _rerank_client = None
