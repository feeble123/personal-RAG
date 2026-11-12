"""本体检索编排：问题 → 实体链接 → 推荐卡/子图 → 事实包 → 不够再回退 RAG。

设计原则：

1. **先查本体**：参数先验、图上关系这类结构化事实，普通向量检索给不了；
2. **不够才回退**：事实条数 < `ONTOLOGY_MIN_FACTS` 时调用 `rag.retrieve`，
   并在响应里明确标 `fallback_used=true`——调用方（agent）能据此判断答案的来源；
3. **不吞异常**：本体侧任何一步失败只降级该步，回退链路照常工作；
4. 事实包里的每条事实都带 `source`（ontology / graph / rag）和证据。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology.cards import build_card
from app.services.graph.ontology.linking import (
    LinkedEntity,
    link_graph_entities,
    link_instances,
)
from app.services.graph.ontology.registry import get_registry

logger = logging.getLogger(__name__)

_MAX_LINKED = 3  # 一个问题最多展开几个实体，防止事实包爆炸


@dataclass
class FactPack:
    question: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False
    sources: list[str] = field(default_factory=list)
    latency_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "facts": self.facts,
            "entities": self.entities,
            "evidence": self.evidence,
            "fallback_used": self.fallback_used,
            "sources": self.sources,
            "latency_ms": self.latency_ms,
            "notes": self.notes,
        }


def _add_source(pack: FactPack, name: str) -> None:
    if name not in pack.sources:
        pack.sources.append(name)


async def _parameter_facts(
    db: AsyncSession,
    question: str,
    *,
    profile_id: str,
    kb_id: int | None,
    conditions: dict[str, str] | None,
) -> tuple[list[dict], list[LinkedEntity]]:
    rows = await inst_mod.list_instances(
        db, profile_id=profile_id, entity_type="Parameter", status="active", limit=500
    )
    linked = link_instances(rows, question)
    if not linked:
        return [], []

    by_uid = {r.uid: r for r in rows}
    facts: list[dict] = []
    for entity in linked[:_MAX_LINKED]:
        row = by_uid.get(entity.uid)
        if row is None:
            continue
        card = build_card(row, conditions)
        fact = {
            "kind": "parameter_prior",
            "subject": card.parameter,
            "name": card.name,
            "group": card.group,
            "unit": card.unit,
            "range": card.range,
            "range_level": card.range_level,
            "initial": card.initial,
            "confidence": card.confidence,
            "evidence_level": card.evidence_level,
            "narrowed_by": card.narrowed_by,
            "evidence": card.evidence,
            "notes": card.notes,
            "matched_text": entity.matched,
            "match_score": round(entity.score, 3),
            "source": "ontology",
        }
        facts.append(fact)
    return facts, linked[:_MAX_LINKED]


async def _graph_facts(
    db: AsyncSession, question: str, *, kb_id: int | None
) -> tuple[list[dict], list[LinkedEntity], list[dict]]:
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        return [], [], []
    linked = await link_graph_entities(db, question, kb_id=kb_id)
    if not linked:
        return [], [], []

    facts: list[dict] = []
    evidence: list[dict] = []
    for entity in linked[:_MAX_LINKED]:
        node = await store.get_node(entity.uid)
        if node is None:
            continue
        facts.append(
            {
                "kind": "graph_node",
                "subject": node.uid,
                "name": node.name,
                "entity_type": node.type_name,
                "props": node.props,
                "matched_text": entity.matched,
                "source": "graph",
            }
        )
        slice_ = await store.neighbors(entity.uid, depth=1, limit=30)
        names = {n.uid: n.name for n in slice_.nodes}
        for edge in slice_.edges[:10]:
            facts.append(
                {
                    "kind": "graph_edge",
                    "subject": names.get(edge.subject_uid, edge.subject_uid),
                    "subject_uid": edge.subject_uid,
                    "predicate": edge.predicate_name,
                    "object": names.get(edge.object_uid, edge.object_uid),
                    "object_uid": edge.object_uid,
                    "evidence_text": edge.evidence_text,
                    "chunk_ids": edge.chunk_ids,
                    "source": "graph",
                }
            )
            if edge.evidence_text or edge.chunk_ids:
                evidence.append(
                    {
                        "kind": "graph_edge",
                        "subject": names.get(edge.subject_uid, edge.subject_uid),
                        "predicate": edge.predicate_name,
                        "object": names.get(edge.object_uid, edge.object_uid),
                        "text": edge.evidence_text,
                        "chunk_ids": edge.chunk_ids,
                    }
                )
    return facts, linked[:_MAX_LINKED], evidence


async def _rag_facts(db: AsyncSession, question: str, kb_id: int | None) -> list[dict]:
    """回退到原有向量+BM25 检索（不改 rag.py，只当调用方）。"""
    from app.services import rag

    chunks = await rag.retrieve(db, question, kb_id=kb_id, top_k=5)
    facts: list[dict] = []
    for item in chunks:
        facts.append(
            {
                "kind": "text_chunk",
                "doc_id": getattr(item, "doc_id", None),
                "chunk_id": getattr(item, "chunk_id", None),
                "doc_name": getattr(item, "doc_name", None),
                "page": getattr(item, "page", None),
                "section": getattr(item, "section", None),
                "snippet": (getattr(item, "content", "") or "")[:300],
                "score": getattr(item, "score", None),
                "source": "rag",
            }
        )
    return facts


async def retrieve_with_ontology(
    db: AsyncSession,
    question: str,
    *,
    kb_id: int | None = None,
    profile_id: str | None = None,
    conditions: dict[str, str] | None = None,
    min_facts: int | None = None,
    allow_fallback: bool = True,
) -> FactPack:
    """本体优先的检索编排。任何一环失败都不抛给调用方，只降级。"""
    started = time.perf_counter()
    reg = get_registry(profile_id or settings.ontology_profile)
    pack = FactPack(question=question)

    try:
        facts, param_entities = await _parameter_facts(
            db, question, profile_id=reg.profile_id, kb_id=kb_id, conditions=conditions
        )
        if facts:
            _add_source(pack, "ontology")
        pack.facts.extend(facts)
        pack.entities.extend(e.to_dict() for e in param_entities)
        for fact in facts:
            pack.evidence.extend(fact.get("evidence") or [])
    except Exception as exc:  # noqa: BLE001 - 本体侧失败不该拖垮问答
        logger.warning("本体参数事实获取失败：%s", exc)
        pack.notes.append("本体查询失败，已降级")

    try:
        g_facts, g_entities, g_evidence = await _graph_facts(db, question, kb_id=kb_id)
        if g_facts:
            _add_source(pack, "graph")
        pack.facts.extend(g_facts)
        pack.entities.extend(e.to_dict() for e in g_entities)
        pack.evidence.extend(g_evidence)
    except Exception as exc:  # noqa: BLE001
        logger.warning("图事实获取失败：%s", exc)
        pack.notes.append("图库查询失败，已降级")

    threshold = settings.ontology_min_facts if min_facts is None else min_facts
    if len(pack.facts) < threshold and allow_fallback:
        try:
            rag_chunks = await _rag_facts(db, question, kb_id)
            if rag_chunks:
                _add_source(pack, "rag")
            pack.facts.extend(rag_chunks)
            pack.fallback_used = True
            pack.notes.append(
                f"本体只给出 {len(pack.facts) - len(rag_chunks)} 条事实（阈值 {threshold}），已回退向量检索"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("回退检索失败：%s", exc)
            pack.notes.append("回退检索失败")

    pack.latency_ms = int((time.perf_counter() - started) * 1000)
    _record_metrics(pack)
    return pack


def _record_metrics(pack: FactPack) -> None:
    try:
        from app.core.metrics import metrics

        result = "fallback" if pack.fallback_used else "ontology"
        metrics["ontology_queries_total"].labels(result=result).inc()
        metrics["ontology_facts_total"].inc(len(pack.facts))
    except Exception:  # noqa: BLE001 - 指标失败不影响业务
        return
