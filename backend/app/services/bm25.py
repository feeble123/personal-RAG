"""BM25 全文检索语料（rank_bm25 + jieba 中文分词）。

- 按知识库维护 BM25Okapi 实例
- 入库/删除后按 kb 重建（答辩规模数千 chunk，重建开销可忽略）
- 与向量检索在 RAG 层做 RRF 融合
"""
from __future__ import annotations

import logging
import threading

import jieba
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

# kb_id -> BM25Okapi
_bm25: dict[int, BM25Okapi] = {}
# kb_id -> list[chunk_id]（与 BM25 语料顺序对齐）
_ids: dict[int, list[int]] = {}
_lock = threading.Lock()


def tokenize(text: str) -> list[str]:
    """jieba 中文分词 + 小写化。"""
    return [w.lower() for w in jieba.lcut(text) if w.strip()]


def rebuild(kb_id: int, items: list[tuple[int, str]]) -> None:
    """重建某知识库语料。items = [(chunk_id, content)]。"""
    with _lock:
        _ids[kb_id] = [cid for cid, _ in items]
        corpus = [tokenize(content) for _, content in items]
        _bm25[kb_id] = BM25Okapi(corpus) if corpus else None


def remove_kb(kb_id: int) -> None:
    with _lock:
        _bm25.pop(kb_id, None)
        _ids.pop(kb_id, None)


def has_kb(kb_id: int) -> bool:
    return _bm25.get(kb_id) is not None


def all_kb_ids() -> list[int]:
    return list(_bm25.keys())


def search(kb_id: int, query: str, k: int = 50) -> list[tuple[int, float]]:
    """返回 [(chunk_id, score)]，按分数降序。"""
    index = _bm25.get(kb_id)
    if not index or not _ids.get(kb_id):
        return []
    q = tokenize(query)
    if not q:
        return []
    scores = index.get_scores(q)
    # (chunk_id, score) 排序
    pairs = sorted(zip(_ids[kb_id], scores), key=lambda x: x[1], reverse=True)
    return [(cid, float(score)) for cid, score in pairs[:k]]
