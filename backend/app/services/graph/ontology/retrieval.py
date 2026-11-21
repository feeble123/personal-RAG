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
from app.services.graph.ontology import rules as rules_mod
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
    #: 冲突仲裁记录：同一工况下多条互相打架的结论，按权威级取了哪条、弃了哪条
    conflicts: list[dict[str, Any]] = field(default_factory=list)
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
            "conflicts": self.conflicts,
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
    seed_types: set[str] | None = None,
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

    `seed_types`：种子的实体类型。用来挡住**反向爬回同类兄弟**——
    问「Ⅳ级响应由谁执行」时问题里没有工况（`forward_only` 不会开），
    两步就会顺着共用的措施走到「Ⅲ级响应」，再展开它那一级的措施；
    实测答案里混进另一个等级整整 10 步。砍的只是"回到另一条同类实例"的边，
    反方向展开别的类型（单位 ← 措施）照旧。
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

    if seed_types and not forward_only:
        seed_set = set(seed_uids)

        def _sibling(uid: str) -> bool:
            """不是种子、但和种子同类型——就是"另一条兄弟实例"。"""
            return (
                uid not in seed_set
                and getattr(nodes.get(uid), "type_name", None) in seed_types
            )

        # **两端都要挡**。只挡主语会漏掉 `其它等级 ← TRIGGERS ← 某个工况` 这种边：
        # 主语是工况（不是兄弟），宾语才是另一条等级，于是它照样混进链路。
        collected = [
            (hop, edge)
            for hop, edge in collected
            if not (_sibling(edge.subject_uid) or _sibling(edge.object_uid))
        ]
    return collected


async def _subsumption_ancestors(
    store: Any, seed_uids: list[str], subsumption_rels: tuple[str, ...]
) -> dict[str, str]:
    """沿声明的包含关系往上走一跳：返回 `{祖先 uid: 子节点 uid}`。

    为什么需要：`INSTANCE_OF` 以前只是一条普通边——不传递、也不继承。
    「20年一遇洪水 属于 10~20年一遇降雨」之后，上一档的调度方案本该自动算作
    20年一遇的，但那要靠遍历恰好走到那个跳数，深度砍到 2 就断了。
    在 TBox 里标了 `subsumption: true` 之后，祖先直接被当成展开起点，
    结果与跳数上限无关。引擎不认识 `INSTANCE_OF` 这个名字。
    """
    if not subsumption_rels or not seed_uids:
        return {}
    out: dict[str, str] = {}
    for uid in seed_uids:
        try:
            slice_ = await store.neighbors(
                uid, depth=1, rel_types=list(subsumption_rels), limit=20
            )
        except Exception:  # noqa: BLE001 - 单点失败不该拖垮整条链路
            logger.warning("包含关系展开失败 uid=%s", uid, exc_info=True)
            continue
        for edge in getattr(slice_, "edges", []):
            if edge.subject_uid == uid and edge.predicate_name in subsumption_rels:
                out.setdefault(edge.object_uid, uid)
    return out


#: **引擎约定**：同一个主语沿同一条关系给出多个结论时，按这个属性取高者。
#: 和 `return_period_low/high` 一样，它是个属性名约定而非领域词——
#: 任何本体只要在实例上声明它，冲突仲裁就自动生效。
AUTHORITY_ATTR = "authority_rank"


def _node_name(nodes: dict[str, Any], uid: str) -> str:
    node = nodes.get(uid)
    return getattr(node, "name", None) or uid


def _arbitrate_conflicts(
    reg: Any, edges: list[tuple[int, Any]], nodes: dict[str, Any]
) -> tuple[list[tuple[int, Any]], list[dict[str, Any]]]:
    """⑥ 冲突仲裁：同一「主语 + 关系」有多条边时，按权威级留一条，其余记成冲突。

    四份报告本来就打架：5~10 年一遇控泄，调度规程写 120 m³/s、汛期调度运用计划写 125。
    以前两条边并存，模型看到两个数，回答里可能两个都抄——读者无从判断该执行哪个。

    三条规矩：
    1. **只有宾语都声明了权威级才仲裁**。有任何一个没声明就不猜，原样保留。
    2. **权威级相同也不猜**：保留全部并标"无法仲裁"。两份同等效力的文件冲突，
       那是要人来定的，不是程序能拍板的。
    3. 被淘汰的边**不删数据**，只从这次链路里拿掉，并记进 `conflicts` 交给回答层
       说明——绝不悄悄丢掉。
    """
    groups: dict[tuple[str, str], list[tuple[int, Any]]] = {}
    for hop, edge in edges:
        groups.setdefault((edge.subject_uid, edge.predicate_name), []).append((hop, edge))

    dropped: set[tuple[str, str, str]] = set()
    conflicts: list[dict[str, Any]] = []
    for (subject_uid, predicate), items in groups.items():
        if len(items) < 2:
            continue
        ranked: list[tuple[float, int, Any]] = []
        for hop, edge in items:
            node = nodes.get(edge.object_uid)
            rank = conditions_mod.as_float(
                (getattr(node, "props", None) or {}).get(AUTHORITY_ATTR)
            )
            if rank is None:  # 有一个没声明权威级 → 整组不仲裁
                ranked = []
                break
            ranked.append((rank, hop, edge))
        if len(ranked) < 2:
            continue

        ranked.sort(key=lambda item: -item[0])
        best_rank = ranked[0][0]
        winners = [item for item in ranked if item[0] == best_rank]
        resolved = len(winners) < len(ranked)
        kept_edge = winners[0][2] if resolved else None

        if resolved:
            for _rank, _hop, edge in ranked:
                if edge is not kept_edge:
                    dropped.add((edge.subject_uid, edge.predicate_name, edge.object_uid))

        conflicts.append(
            {
                "subject": _node_name(nodes, subject_uid),
                "predicate": predicate,
                "predicate_label": relation_label_zh(predicate),
                "resolved": resolved,
                "reason": "按权威级取高者" if resolved else "参与冲突的方案权威级相同，无法自动仲裁",
                "chosen": (
                    {
                        "name": _node_name(nodes, kept_edge.object_uid),
                        "props": _labeled_props(nodes, reg, kept_edge.object_uid),
                        "authority_rank": best_rank,
                    }
                    if kept_edge is not None
                    else None
                ),
                "rejected": [
                    {
                        "name": _node_name(nodes, edge.object_uid),
                        "props": _labeled_props(nodes, reg, edge.object_uid),
                        "authority_rank": rank,
                    }
                    for rank, _hop, edge in ranked
                    if edge is not kept_edge
                ],
            }
        )

    if not dropped:
        return edges, conflicts
    kept_edges = [
        item
        for item in edges
        if (item[1].subject_uid, item[1].predicate_name, item[1].object_uid) not in dropped
    ]
    return kept_edges, conflicts


def _declared_phase(reg: Any, predicate: str) -> str:
    """TBox 里给这条关系声明的链路档位；没声明返回空（编排层回落到内置表）。"""
    rel = reg.relation(predicate) if reg is not None else None
    return getattr(rel, "phase", "") or ""


#: 引擎内部字段，**不进答案正文**。
#: `expected_measures` 是"预案这一级原文共几条"的建库口径（给 coverage 约束用的），
#: `authority_rank` 是冲突仲裁的排序值。两者都不是给读者看的业务事实——
#: 放进去的结果是答案里冒出「预案该级列出的处置措施条数：6」这种话（实测）。
INTERNAL_PROPS = ("expected_measures", "authority_rank")


def _labeled_props(nodes: dict[str, Any], reg: Any, uid: str) -> dict[str, Any]:
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
        if key.endswith("_count") or key == "display_zh" or key in INTERNAL_PROPS:
            continue
        if value in (None, ""):
            continue
        attr = entity.attributes.get(key) if entity is not None else None
        out[(attr.description if attr and attr.description else key)] = value
    return out


def _label_edge_props(
    nodes: dict[str, Any], reg: Any, object_uid: str, props: dict[str, Any] | None
) -> dict[str, Any]:
    """边属性 → 中文键，**只收 TBox 里声明过的**。

    边属性是「随主语变」的值：同一条措施在Ⅱ级是「30min 内」、在Ⅳ级是「1h 内」。
    挂在节点上就会串味——实测回答里出现「时限：Ⅳ/Ⅲ级 1h 内；Ⅱ级 30min 内」，
    读者得自己从四个等级里挑一个。

    `source_item` 这类内部字段没有声明，不进正文。
    """
    node = nodes.get(object_uid)
    entity = reg.entity(getattr(node, "type_name", "")) if reg is not None else None
    out: dict[str, Any] = {}
    for key, value in (props or {}).items():
        attr = entity.attributes.get(key) if entity is not None else None
        if attr is None or value in (None, ""):
            continue
        out[attr.description or key] = value
    return out


async def _derive_facts(
    db: AsyncSession,
    reg: Any,
    conds: list[conditions_mod.Condition],
    subject_entities: list[LinkedEntity],
    nodes: dict[str, Any],
    existing: set[tuple[str, str]],
) -> list[dict]:
    """② 规则推导：工况数值满足规则时，产出"这个工况 → 结论实例"的边。

    这些边**不在数据库里**，是当场算出来的。这就是本体相对 GraphRAG 的差别：
    300 年一遇没人手工连过边，但只要规则说「(50, 1000] 归 Ⅱ级」，它就答得出来。

    证据用结论实例自己的：`Ⅱ级响应` 的出处那一页，正是预案写"可能遭遇 50 年以上
    1000 年以下一遇的洪水"的那一节——推导的正当性就来自那里。
    """
    if reg is None or not reg.rules or not conds:
        return []
    hits = rules_mod.matching_rules(reg, conds)
    if not hits:
        return []

    # 主体：优先挂在链接到的工况实例上（"300年一遇洪水（正常年）"）；
    # 没链接到就退回问题里的原话，绝不硬造一个像实例的 uid。
    if subject_entities:
        subject_name = subject_entities[0].name
        subject_uid = subject_entities[0].uid
    else:
        subject_name = hits[0].condition.raw
        subject_uid = f"question:{subject_name}"

    out: list[dict] = []
    cache: dict[str, list[Any]] = {}
    for hit in hits:
        rule = hit.rule
        rows = cache.get(rule.target_type)
        if rows is None:
            rows = await inst_mod.list_instances(
                db,
                profile_id=reg.profile_id,
                entity_type=rule.target_type,
                status="active",
                limit=500,
            )
            cache[rule.target_type] = rows
        target = next((r for r in rows if rules_mod.target_matches(rule, r.props)), None)
        if target is None:
            # 规则命中但目标实例不存在：说明 ABox 缺这一档，记日志不硬凑
            logger.info("规则 %s 命中，但库里没有 %s 实例 %s", rule.id, rule.target_type, rule.target_match)
            continue

        # 去重按「关系 + 结论」而不是「谁 + 关系 + 结论」：
        # 手工连的 `50~1000年一遇 —触发→ Ⅱ级` 和规则推的 `100年一遇 —触发→ Ⅱ级`
        # 对读者是同一句话。**人工写的事实优先**，规则只在链路上还没有这个结论时补位
        # （这正是 CQ-22 要的：没人连过边的工况，靠规则也能归级）。
        key = (rule.relation, target.uid)
        if key in existing:
            continue
        existing.add(key)

        if target.uid not in nodes:
            nodes[target.uid] = inst_mod.to_node(target)
        ev = [e for e in (target.evidence or []) if isinstance(e, dict)]
        chunk_ids = [int(e["chunk_id"]) for e in ev if e.get("chunk_id") is not None]
        out.append(
            {
                "kind": "graph_edge",
                "subject": subject_name,
                "subject_uid": subject_uid,
                "predicate": rule.relation,
                "predicate_label": relation_label_zh(rule.relation),
                "phase": _declared_phase(reg, rule.relation),
                "object": target.name,
                "object_uid": target.uid,
                "object_props": _labeled_props(nodes, reg, target.uid),
                "evidence_text": f"由规则 {rule.id} 推导：{hit.condition.raw}",
                "chunk_ids": chunk_ids,
                "hop": 1,
                "derived_by": rule.id,
                "source": "graph",
            }
        )
    return out


async def _resolve_profile_id(db: AsyncSession, kb_id: int | None) -> str | None:
    """按知识库推断该用哪套本体。

    为什么要有这一步：问答页只传 kb_id，不传 profile。以前一律回落到全局默认
    `ONTOLOGY_PROFILE`，于是问高洞子水库的问题用的却是调参本体——TBox 对不上，
    阶段划分和属性标签全错。这里改成**看数据**：某个库里只灌了一套本体的实例，
    那就用那一套；多套并存时不猜，交回全局默认。
    """
    if db is None or kb_id is None:
        return None
    from sqlalchemy import select

    try:
        rows = (
            await db.execute(
                select(inst_mod.OntologyInstance.profile_id)
                .where(inst_mod.OntologyInstance.kb_id == kb_id)
                .distinct()
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 - 推断失败不该影响问答
        return None
    return rows[0] if len(rows) == 1 else None


def _condition_like_types(reg: Any) -> list[str]:
    """哪些实体类型算「工况」——声明了频率或区间属性的类型。

    这是一个**引擎约定**，不是领域词：任何本体只要在 TBox 里给某个类型声明
    `frequency` / `return_period` / `return_period_low|high` 之类的属性，
    它的实例就会被工况检索当成候选。引擎不需要知道「水库」两个字。
    """
    if reg is None:
        return []
    keys = set(conditions_mod.FREQUENCY_KEYS)
    return [
        name
        for name, entity in reg.entity_types.items()
        if any(attr.name in keys for attr in entity.attributes.values())
    ]


def _compare_semantics(reg: Any, entity_type: str) -> dict[str, str]:
    """这个类型里哪些属性是**阈值**（`compare: gt/gte/lt/lte`）。

    引擎不认识"水位"，阈值语义只能由 TBox 声明；这里只负责把它取出来传下去。
    """
    ent = reg.entity(entity_type) if reg is not None else None
    if ent is None:
        return {}
    return {a.name: a.compare for a in ent.attributes.values() if a.compare}


async def _link_conditions_by_value(
    db: AsyncSession,
    conds: list[conditions_mod.Condition],
    reg: Any,
    *,
    limit: int = _MAX_LINKED,
) -> list[LinkedEntity]:
    """用问题里抽到的工况去**找**实例，而不是等实例名恰好是问题的子串。

    为什么必须补这一步：「20 年以上 50 年以下一遇」是文档的说法，用户只会说
    「50 年一遇」。名字对不上时，原来的链接器一个工况都连不上，链路直接断掉，
    用户看到的就是「本体什么都没给」。
    """
    if db is None or reg is None or not conds:
        return []
    dims = conditions_mod.dimension_values(conds)
    matched_raw = "、".join(c.raw for c in conds if c.raw)
    candidates: list[tuple[tuple, LinkedEntity]] = []
    for entity_type in _condition_like_types(reg):
        rows = await inst_mod.list_instances(
            db, profile_id=reg.profile_id, entity_type=entity_type, status="active", limit=500
        )
        for row in rows:
            compares = _compare_semantics(reg, row.entity_type)
            # **多个条件按 AND 合并**：问题同时给了雨前水位和雨量时，两个维度
            # 都要落在同一个实例上才算命中（见 `conditions.matches_all`）。
            if not conditions_mod.matches_all(row.props, row.name, dims, compares=compares):
                continue
            # 越具体越先讲：精确重现期 > 窄区间 > 宽区间。
            # 「20 年一遇」会同时落在 10~20、20~50 两档上，不排序的话
            # 讲哪个全看遍历顺序，答案就不稳定了。
            props = row.props or {}
            years = dims.get("return_period")
            exact = years is not None and conditions_mod.as_float(props.get("return_period")) == years
            low = conditions_mod.as_float(props.get("return_period_low"))
            high = conditions_mod.as_float(props.get("return_period_high"))
            width = abs(high - low) if (low is not None and high is not None) else 1e9
            key = (0 if exact else 1, width, row.name)
            candidates.append(
                (
                    key,
                    LinkedEntity(
                        uid=row.uid,
                        entity_type=row.entity_type,
                        name=row.name,
                        display=row.name,
                        matched=matched_raw,
                        score=1.0 if exact else 0.9,
                        source="instance",
                    ),
                )
            )
    candidates.sort(key=lambda item: item[0])
    return [entity for _key, entity in candidates[:limit]]


async def _graph_facts(
    db: AsyncSession,
    question: str,
    *,
    kb_id: int | None,
    question_conditions: list[conditions_mod.Condition] | None = None,
    reg: Any = None,
) -> tuple[list[dict], list[LinkedEntity], list[dict], list[dict]]:
    """返回 (事实, 链接到的实体, 证据, 冲突仲裁记录)。"""
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        return [], [], [], []

    # 工况条件（20 年一遇 / P=5% / 3200 m³/s）决定"讲哪个分支的方案"，
    # 命中工况的节点排在前面，并标出来供编排层使用。
    conds = (
        question_conditions
        if question_conditions is not None
        else conditions_mod.extract_conditions(question)
    )

    linked = await link_graph_entities(db, question, kb_id=kb_id)
    matched_by_value = await _link_conditions_by_value(db, conds, reg)
    if matched_by_value:
        seen = {e.uid for e in matched_by_value}
        linked = matched_by_value + [e for e in linked if e.uid not in seen]
    if not linked:
        return [], [], [], []

    facts: list[dict] = []
    evidence: list[dict] = []
    nodes: dict[str, Any] = {}  # uid → Node，边两端的名字从这里取

    seeds = linked[:_MAX_LINKED]
    # 联合匹配（雨前水位 + 雨量 + 年型）命中过的实例，这里要认账：
    # 旧的 `any_match` 一次只看一条条件，对「干旱年 140mm」这种**两个维度凑在
    # 一起才成立**的工况会判 False，于是它被排到后面、也不开 `forward_only`，
    # 同类型的兄弟工况跟着被展开进答案。
    matched_by_value_uids = {e.uid for e in matched_by_value}
    node_facts: list[dict] = []
    for entity in seeds:
        node = await store.get_node(entity.uid)
        if node is None:
            continue
        nodes[node.uid] = node
        matched = conditions_mod.any_match(
            node.props,
            node.name,
            conds,
            compares=_compare_semantics(reg, node.type_name),
        ) or entity.uid in matched_by_value_uids
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

    # ③ 包含推理：把种子的祖先也当成展开起点（只沿 TBox 声明的包含关系走）。
    # 祖先节点要补进 nodes，否则边两端的名字取不到、属性标签也拼不出来。
    subsumption_rels = reg.subsumption_relations() if reg is not None else ()
    ancestors = await _subsumption_ancestors(store, list(frontier_seeds), subsumption_rels)
    for ancestor_uid in ancestors:
        if ancestor_uid not in nodes:
            node = await store.get_node(ancestor_uid)
            if node is not None:
                nodes[ancestor_uid] = node

    edges = await _expand_graph(
        store,
        list(dict.fromkeys([*frontier_seeds, *ancestors])),
        nodes=nodes,
        depth=depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        rel_types=rel_types,
        # 命中工况时只顺着"处置方向"走，别从共用措施爬回其它等级
        forward_only=bool(matched_uids),
        # 没命中工况时也还有一道闸：别反向爬回**同类型的兄弟实例**
        seed_types={
            getattr(nodes.get(uid), "type_name", None) for uid in frontier_seeds
        }
        - {None},
    )

    # ⑥ 冲突仲裁：同一「主语 + 关系」有多条边时按权威级留一条（如 120 vs 125）。
    # 必须在建 names 之前——被淘汰的方案节点不该出现在这一轮的节点表里。
    edges, conflicts = _arbitrate_conflicts(reg, edges, nodes)

    # ② 规则推导：数据库里没有的边，按 TBox 规则当场算出来。
    # 必须在建 names 之前调用——它会把结论节点补进 nodes。
    seen_keys = {(e.predicate_name, e.object_uid) for _hop, e in edges}
    derived = await _derive_facts(db, reg, conds, matched_by_value, nodes, seen_keys)

    names = {uid: node.name for uid, node in nodes.items()}
    ancestor_names = {uid: names.get(child, child) for uid, child in ancestors.items()}

    for hop, edge in edges:
        inherited_from = ancestor_names.get(edge.subject_uid)
        # 宾语属性：节点级是"通用值"，边级是"本等级的值"。边级覆盖节点级——
        # 「传达命令在Ⅱ级 30min 内」这种差异只能挂在边上，节点上只能写一句笼统的。
        object_props = _labeled_props(nodes, reg, edge.object_uid)
        object_props.update(
            _label_edge_props(nodes, reg, edge.object_uid, getattr(edge, "props", None))
        )
        facts.append(
            {
                "kind": "graph_edge",
                "subject": names.get(edge.subject_uid, edge.subject_uid),
                "subject_uid": edge.subject_uid,
                "predicate": edge.predicate_name,
                "predicate_label": relation_label_zh(edge.predicate_name),
                "phase": _declared_phase(reg, edge.predicate_name),
                "object": names.get(edge.object_uid, edge.object_uid),
                "object_uid": edge.object_uid,
                # 宾语节点自己的属性跟着走：链路里只报名字的话，
                # 「10~20年一遇控泄方案」看不出到底泄多少。
                "object_props": object_props,
                "evidence_text": edge.evidence_text,
                "chunk_ids": edge.chunk_ids,
                # 边自己的属性：`source_item`（对应原文第几条）就在这里，
                # 编排层拿它按原文顺序排措施，而不是按发现顺序。
                "edge_props": dict(getattr(edge, "props", None) or {}),
                "hop": hop,
                # 从祖先继承来的事实：来源是"A 属于 B"里的 B
                "inherited_from": inherited_from,
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

    facts.extend(derived)
    for fact in derived:
        evidence.append(
            {
                "kind": "graph_edge",
                "subject": fact["subject"],
                "predicate": fact["predicate"],
                "object": fact["object"],
                "text": fact["evidence_text"],
                "chunk_ids": fact["chunk_ids"],
                "hop": fact["hop"],
            }
        )

    return facts, linked[:_MAX_LINKED], evidence, conflicts


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
    resolved = profile_id or await _resolve_profile_id(db, kb_id) or settings.ontology_profile
    reg = get_registry(resolved)
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
        g_facts, g_entities, g_evidence, g_conflicts = await _graph_facts(
            db, question, kb_id=kb_id, question_conditions=question_conditions, reg=reg
        )
        if g_facts:
            _add_source(pack, "graph")
        pack.facts.extend(g_facts)
        pack.entities.extend(e.to_dict() for e in g_entities)
        pack.evidence.extend(g_evidence)
        pack.conflicts.extend(g_conflicts)
        for item in g_conflicts:
            if not item.get("resolved"):
                pack.notes.append(
                    f"「{item['subject']}」有 {1 + len(item['rejected'])} 条"
                    f"{item['predicate_label']}，权威级相同无法自动仲裁"
                )

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
