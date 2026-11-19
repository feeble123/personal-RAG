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
from app.services.graph.ontology import chain as chain_mod
from app.services.graph.ontology import conditions as conditions_mod
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology.cards import build_card
from app.services.graph.ontology.linking import (
    LinkedEntity,
    link_graph_entities,
    link_instances,
)
from app.services.graph.ontology.registry import get_registry
from app.services.graph.schema import entity_label_zh, relation_label_zh

logger = logging.getLogger(__name__)

_MAX_LINKED = 3  # 一个问题最多展开几个实体，防止事实包爆炸


@dataclass
class FactPack:
    question: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False
    #: 链路骨架（把多跳事实按人讲的顺序排好，附带证据）；没有图边时为 None
    chain: dict[str, Any] | None = None
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
            "chain": self.chain,
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


async def _resolve_chunk_evidence(db: AsyncSession, chunk_ids: list[int]) -> dict[int, dict]:
    """chunk_id → 文档名 / 页码 / 片段。链路每一步都要能点回原文，所以必须先解析。"""
    if not chunk_ids or db is None:
        return {}
    from sqlalchemy import select

    from app.db.models import Chunk, Document

    rows = (
        await db.execute(
            select(Chunk.id, Chunk.page, Chunk.printed_page, Chunk.content, Document.filename)
            .join(Document, Document.id == Chunk.doc_id)
            .where(Chunk.id.in_(chunk_ids))
        )
    ).all()
    return {
        int(r[0]): {
            "chunk_id": int(r[0]),
            "doc_name": r[4] or "",
            "page": r[1],
            "printed_page": r[2],
            "snippet": (r[3] or "")[:160],
        }
        for r in rows
    }


def _graph_limits() -> tuple[int, int, int, list[Any] | None]:
    """读取图上展开的边界。深度的硬上限写在这里，配置写错也炸不了图。"""
    depth = max(1, min(int(settings.ontology_graph_depth), int(settings.ontology_graph_max_depth)))
    max_nodes = max(1, int(settings.ontology_graph_max_nodes))
    max_edges = max(1, int(settings.ontology_graph_max_edges))
    raw = (settings.ontology_graph_relations or "").strip()
    rel_types = [item.strip() for item in raw.split(",") if item.strip()] or None
    return depth, max_nodes, max_edges, rel_types


async def _expand_graph(
    store: Any,
    seed_uids: list[str],
    *,
    nodes: dict[str, Any],
    depth: int,
    max_nodes: int,
    max_edges: int,
    rel_types: list[Any] | None,
    forward_only: bool = False,
) -> list[tuple[int, Any]]:
    """从种子出发逐跳展开，返回 [(跳数, 边)]。

    为什么逐跳而不是直接 `neighbors(depth=N)`：

    1. 存储层的内存实现把深度钳在 1~2 跳，逐跳调用才不受它限制；
    2. 链路问题的图是"长而窄"的（水库→方案→动作→村镇→响应），逐跳可以
       在每一跳上限制节点数，避免某一跳扇出几百个下游村把响应撑爆；
    3. 每跳的边都带 hop，编排回答时才能按顺序排（下一单元要用）。

    `forward_only`：只顺着边的方向走（当前节点必须是主语）。
    问题里点了具体工况时必须开——处置链是有方向的，而"通知下游"这类措施会被
    Ⅰ/Ⅱ/Ⅲ/Ⅳ 四级共用，双向展开会顺着它们爬到别的等级，把「20 年一遇」
    的答案混进「1000 年一遇」才有的动作。
    """
    seen_edges: set[tuple[str, str, str]] = set()
    collected: list[tuple[int, Any]] = []
    visited = set(seed_uids)
    frontier = list(seed_uids)

    for hop in range(1, depth + 1):
        if not frontier or len(collected) >= max_edges or len(visited) >= max_nodes:
            break
        next_frontier: list[str] = []
        for uid in frontier:
            try:
                slice_ = await store.neighbors(uid, depth=1, rel_types=rel_types, limit=60)
            except Exception:  # noqa: BLE001 - 单点失败不该拖垮整条链路
                logger.warning("图谱展开失败 uid=%s", uid, exc_info=True)
                continue
            edges = list(getattr(slice_, "edges", []))
            if forward_only:
                edges = [e for e in edges if e.subject_uid == uid]
            for node in getattr(slice_, "nodes", []):
                nodes.setdefault(node.uid, node)
                if node.uid not in visited and len(visited) < max_nodes:
                    visited.add(node.uid)
                    if not forward_only or any(e.object_uid == node.uid for e in edges):
                        next_frontier.append(node.uid)
            for edge in edges:
                key = (edge.subject_uid, edge.predicate_name, edge.object_uid)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                collected.append((hop, edge))
                if len(collected) >= max_edges:
                    break
            if len(collected) >= max_edges:
                break
        frontier = next_frontier

    return collected


async def _graph_facts(
    db: AsyncSession,
    question: str,
    *,
    kb_id: int | None,
    question_conditions: list[conditions_mod.Condition] | None = None,
    reg: Any = None,
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
    nodes: dict[str, Any] = {}  # uid → Node，边两端的名字从这里取

    # 工况条件（20 年一遇 / P=5% / 3200 m³/s）决定"讲哪个分支的方案"，
    # 命中工况的节点排在前面，并标出来供编排层使用。
    conds = (
        question_conditions
        if question_conditions is not None
        else conditions_mod.extract_conditions(question)
    )
    seeds = linked[:_MAX_LINKED]
    node_facts: list[dict] = []
    for entity in seeds:
        node = await store.get_node(entity.uid)
        if node is None:
            continue
        nodes[node.uid] = node
        matched = conditions_mod.any_match(node.props, node.name, conds)
        fact = {
            "kind": "graph_node",
            "subject": node.uid,
            "name": node.name,
            "entity_type": node.type_name,
            "entity_type_label": entity_label_zh(node.type_name),
            "props": node.props,
            "matched_text": entity.matched,
            "hop": 0,
            "condition_matched": matched,
            "source": "graph",
        }
        if matched:
            fact["conditions"] = [c.to_dict() for c in conds]
        node_facts.append(fact)
    # 命中工况的先讲，其余保持链接顺序
    facts.extend(sorted(node_facts, key=lambda f: 0 if f.get("condition_matched") else 1))

    depth, max_nodes, max_edges, rel_types = _graph_limits()
    # 剪枝：如果问题指定了工况、且有节点命中，就只从命中的那个分支往外走。
    # 否则同一座水库的 5% 与 1% 两套方案会被一起展开，答案里混着两个方案。
    matched_uids = [f["subject"] for f in facts if f.get("condition_matched")]
    frontier_seeds = matched_uids or list(nodes)
    edges = await _expand_graph(
        store,
        frontier_seeds,
        nodes=nodes,
        depth=depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        rel_types=rel_types,
        # 命中工况时只顺着"处置方向"走，别从共用措施爬回其它等级
        forward_only=bool(matched_uids),
    )

    names = {uid: node.name for uid, node in nodes.items()}

    def _declared_phase(predicate: str) -> str:
        """TBox 里给这条关系声明的链路档位；没声明返回空（编排层回落到内置表）。"""
        rel = reg.relation(predicate) if reg is not None else None
        return getattr(rel, "phase", "") or ""

    def _labeled_props(uid: str) -> dict[str, Any]:
        """宾语节点的属性，键换成 TBox 里的中文说明。

        为什么不直接给 `outflow_m3s=130`：这套属性最终会喂给模型写成一段话，
        英文键会被原样抄进正文（实测：「该方案为控泄模式、outflow_m3s=130」）。
        中文键来自 YAML 的 attribute.description，读起来就是人话。
        """
        node = nodes.get(uid)
        props = dict(getattr(node, "props", None) or {})
        entity = reg.entity(getattr(node, "type_name", "")) if reg is not None else None
        out: dict[str, Any] = {}
        for key, value in props.items():
            if key.endswith("_count") or key == "display_zh" or value in (None, ""):
                continue
            attr = entity.attributes.get(key) if entity is not None else None
            out[(attr.description if attr and attr.description else key)] = value
        return out

    for hop, edge in edges:
        facts.append(
            {
                "kind": "graph_edge",
                "subject": names.get(edge.subject_uid, edge.subject_uid),
                "subject_uid": edge.subject_uid,
                "predicate": edge.predicate_name,
                "predicate_label": relation_label_zh(edge.predicate_name),
                "phase": _declared_phase(edge.predicate_name),
                "object": names.get(edge.object_uid, edge.object_uid),
                "object_uid": edge.object_uid,
                # 宾语节点自己的属性跟着走：链路里只报名字的话，
                # 「10~20年一遇控泄方案」看不出到底泄多少。
                "object_props": _labeled_props(edge.object_uid),
                "evidence_text": edge.evidence_text,
                "chunk_ids": edge.chunk_ids,
                "hop": hop,
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
                    "hop": hop,
                }
            )

    return facts, linked[:_MAX_LINKED], evidence


async def _rag_facts(db: AsyncSession, question: str, kb_id: int | None) -> list[dict]:
    """回退到原有向量+BM25 检索（不改 rag.py，只当调用方）。"""
    from app.services import rag

    chunks = await rag.retrieve(db, question, kb_id=kb_id, top_k=5)
    facts: list[dict] = []
    for item in chunks:
        # RetrievedChunk 的正文在 snippet 上（不是 content），取错字段会得到一堆空事实
        facts.append(
            {
                "kind": "text_chunk",
                "doc_id": getattr(item, "doc_id", None),
                "chunk_id": getattr(item, "chunk_id", None),
                "page": getattr(item, "page", None),
                "section": getattr(item, "section", None),
                "snippet": (getattr(item, "snippet", "") or "")[:300],
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

    # 工况条件（20 年一遇 / P=5% / 3200 m³/s）：同一座水库不同频率的方案完全不同，
    # 先抽出来，图上命中它的节点排前面，也让调用方看到程序是怎么理解问题的。
    question_conditions = conditions_mod.extract_conditions(question)
    if question_conditions:
        pack.notes.append("识别到工况：" + "、".join(c.describe() for c in question_conditions))

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
        g_facts, g_entities, g_evidence = await _graph_facts(
            db, question, kb_id=kb_id, question_conditions=question_conditions, reg=reg
        )
        if g_facts:
            _add_source(pack, "graph")
        pack.facts.extend(g_facts)
        pack.entities.extend(e.to_dict() for e in g_entities)
        pack.evidence.extend(g_evidence)

        # 把图事实排成链路骨架（顺序与证据由程序定死，措辞以后再说）
        edge_chunk_ids = [
            int(cid)
            for fact in g_facts
            if fact.get("kind") == "graph_edge"
            for cid in (fact.get("chunk_ids") or [])
            if str(cid).isdigit()
        ]
        resolved = await _resolve_chunk_evidence(db, sorted(set(edge_chunk_ids)))
        built = chain_mod.build_chain(
            g_facts,
            conditions=[c.describe() for c in question_conditions],
            evidence_lookup=lambda ids: [resolved[i] for i in ids if i in resolved],
        )
        if built.steps:
            pack.chain = built.to_dict()
            if built.gaps:
                pack.notes.append(f"链路有 {len(built.gaps)} 个环节没有证据，已单列")
    except Exception as exc:  # noqa: BLE001
        logger.warning("图事实获取失败：%s", exc)
        pack.notes.append("图库查询失败，已降级")

    # 回退判定只数「本体/图谱给了几条事实」，必须在拼接 RAG 之前算，否则数的是回退后的总数。
    # 一个参数问题天然只有 1 条参数卡（范围/初值/敏感度/依据都在卡里），
    # 门槛设成 3 会把每一个参数问题都误判成"本体不够"——这正是之前 22 秒回退的成因。
    threshold = settings.ontology_min_facts if min_facts is None else min_facts
    ontology_facts = len(pack.facts)
    if ontology_facts < threshold and allow_fallback:
        try:
            rag_chunks = await _rag_facts(db, question, kb_id)
            if rag_chunks:
                _add_source(pack, "rag")
            pack.facts.extend(rag_chunks)
            pack.fallback_used = True
            pack.notes.append(
                f"本体只给出 {ontology_facts} 条事实（阈值 {threshold}），已回退向量检索"
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
