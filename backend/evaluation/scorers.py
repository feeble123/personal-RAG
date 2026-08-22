"""P1-1 评测门禁：确定性 scorer（无 LLM，可重复）。

- recall_at_k：gold 的 expect_keywords 是否命中检索 top-k（任一个 chunk 含任一关键词）
- citation_hit：检索结果中是否命中带出处（section/page 非空）的 chunk
- intent 分组统计：按 intent 分别聚合 Recall@5/10
"""
from __future__ import annotations

from app.services.rag import RetrievedChunk


def chunk_hits_keyword(chunk: RetrievedChunk, keywords: list[str]) -> bool:
    """chunk 内容（snippet）是否包含任一关键词。"""
    text = (chunk.snippet or "") + " " + (chunk.section or "") + " " + (chunk.source or "")
    return any(kw in text for kw in keywords)


def recall_at_k(cites: list[RetrievedChunk], keywords: list[str], k: int) -> bool:
    """top-k 内是否有 chunk 命中任一关键词。"""
    for c in cites[:k]:
        if chunk_hits_keyword(c, keywords):
            return True
    return False


def has_citation(cites: list[RetrievedChunk], k: int) -> bool:
    """top-k 内是否有带出处（section 或 page）的 chunk。"""
    for c in cites[:k]:
        if c.section or c.page:
            return True
    return False


def aggregate(results: list[dict]) -> dict:
    """汇总多问结果：整体 Recall@5/10 + 分 intent。

    results: [{q, kb, intent, recall5, recall10, citation}...]
    """
    total = len(results)
    r5 = sum(1 for r in results if r["recall5"]) / total if total else 0
    r10 = sum(1 for r in results if r["recall10"]) / total if total else 0
    cit = sum(1 for r in results if r["citation"]) / total if total else 0

    by_intent: dict[str, dict] = {}
    for r in results:
        intent = r["intent"]
        b = by_intent.setdefault(intent, {"n": 0, "recall5": 0, "recall10": 0})
        b["n"] += 1
        if r["recall5"]:
            b["recall5"] += 1
        if r["recall10"]:
            b["recall10"] += 1
    for b in by_intent.values():
        b["recall5"] = round(b["recall5"] / b["n"], 4) if b["n"] else 0
        b["recall10"] = round(b["recall10"] / b["n"], 4) if b["n"] else 0

    return {
        "total": total,
        "recall_at_5": round(r5, 4),
        "recall_at_10": round(r10, 4),
        "citation_hit": round(cit, 4),
        "by_intent": by_intent,
    }
