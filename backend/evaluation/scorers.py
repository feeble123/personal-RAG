"""P1-1 评测门禁：确定性 scorer（无 LLM，可重复）。

- recall_at_k（宽松）：gold 的 expect_keywords 是否命中检索 top-k（任一个 chunk 含任一关键词）
- recall_clause_at_k（严格）：gold 的 expect_clauses 条款号是否命中检索 top-k
  （chunk 的 section 或 clause_no 含该条款号）
- citation_hit：检索结果中是否命中带出处（section/page 非空）的 chunk
- intent / kb / doc_type 分组统计：分层暴露短板
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


def chunk_hits_clause(chunk: RetrievedChunk, clauses: list[str]) -> bool:
    """chunk 是否命中任一条款号（section 或 clause_no 含条款号）。

    条款号匹配：`4.3` 匹配 section 里的 "4.3 " / "4.3" / "4.3.1"（前缀），
    但 `4.3` 不匹配 "4.30"（用边界）。
    """
    text = (chunk.section or "") + " " + (chunk.clause_no or "")
    for cl in clauses:
        if not cl:
            continue
        # 精确段匹配或后跟非数字（防 4.3 误中 4.30）
        import re

        if re.search(rf"(^|[\s/（(]){re.escape(cl)}(?![0-9.])", text):
            return True
    return False


def recall_clause_at_k(cites: list[RetrievedChunk], clauses: list[str], k: int) -> bool:
    """top-k 内是否有 chunk 命中任一条款号（严格判定）。clauses 为空时返回 None（不判定）。"""
    if not clauses:
        return None
    for c in cites[:k]:
        if chunk_hits_clause(c, clauses):
            return True
    return False


def has_citation(cites: list[RetrievedChunk], k: int) -> bool:
    """top-k 内是否有带出处（section 或 page）的 chunk。"""
    for c in cites[:k]:
        if c.section or c.page:
            return True
    return False


def aggregate(results: list[dict]) -> dict:
    """汇总多问结果：整体 Recall@5/10 + 严格条款 Recall + 分 intent/kb。

    results: [{q, kb, intent, doc_type, recall5, recall10, clause5, clause10, citation}...]
    clause 为 None（无 expect_clauses）的问题不计入严格指标分母。
    """
    total = len(results)
    r5 = sum(1 for r in results if r["recall5"]) / total if total else 0
    r10 = sum(1 for r in results if r["recall10"]) / total if total else 0
    cit = sum(1 for r in results if r["citation"]) / total if total else 0

    # 严格条款指标（仅 expect_clauses 非空的问题）
    clause_qs = [r for r in results if r.get("clause5") is not None]
    cl_total = len(clause_qs)
    cl5 = sum(1 for r in clause_qs if r["clause5"]) / cl_total if cl_total else None
    cl10 = sum(1 for r in clause_qs if r["clause10"]) / cl_total if cl_total else None

    def _group(keys: tuple[str, ...]):
        """按指定字段分组聚合。"""
        groups: dict[tuple, dict] = {}
        for r in results:
            key = tuple(r.get(k) for k in keys)
            g = groups.setdefault(key, {"n": 0, "recall5": 0, "recall10": 0, "clause5": 0, "clause10": 0, "clause_n": 0})
            g["n"] += 1
            if r["recall5"]:
                g["recall5"] += 1
            if r["recall10"]:
                g["recall10"] += 1
            if r.get("clause5") is not None:
                g["clause_n"] += 1
                if r["clause5"]:
                    g["clause5"] += 1
                if r["clause10"]:
                    g["clause10"] += 1
        for g in groups.values():
            g["recall5"] = round(g["recall5"] / g["n"], 4) if g["n"] else 0
            g["recall10"] = round(g["recall10"] / g["n"], 4) if g["n"] else 0
            if g["clause_n"]:
                g["clause5"] = round(g["clause5"] / g["clause_n"], 4)
                g["clause10"] = round(g["clause10"] / g["clause_n"], 4)
            else:
                g["clause5"] = None
                g["clause10"] = None
        # JSON 序列化要求 key 为字符串：tuple → 单元素取首，多元素用 "/" 连接
        return {
            (k[0] if len(k) == 1 else " / ".join(str(x) for x in k)): v
            for k, v in groups.items()
        }

    by_intent = _group(("intent",))
    by_kb = _group(("kb",))

    return {
        "total": total,
        "recall_at_5": round(r5, 4),
        "recall_at_10": round(r10, 4),
        "citation_hit": round(cit, 4),
        "clause_at_5": round(cl5, 4) if cl5 is not None else None,
        "clause_at_10": round(cl10, 4) if cl10 is not None else None,
        "by_intent": by_intent,
        "by_kb": by_kb,
    }
