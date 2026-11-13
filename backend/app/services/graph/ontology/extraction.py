"""F4 自动抽取：从知识库切片里找属性值，进「待审」暂存区。

**两条腿，规则优先**
1. `run_rule_extraction`：TBox 里给属性配了 `extract.pattern` 正则，就按正则扫切片。
   快、确定、每条都能点回原文；缺点是写法一变就抽不到。
2. `run_llm_extraction`：规则没抽到的实例交给大模型，按属性说明去读散文。
   能读懂各种写法，但慢、费 token、可能编造——所以置信度给得低，且必须人工确认。

**绝不直接写实例**
所有结果先落 `ontology_proposals`，人在界面上点「接受」才写进实例属性并附证据。
证据只增不减的承诺靠的就是这一道闸。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, OntologyInstance, OntologyProposal
from app.services.graph.ontology.registry import AttributeDef, EntityTypeDef, OntologyRegistry

logger = logging.getLogger(__name__)

_MAX_CHUNKS_PER_INSTANCE = 20   # 每个实例最多翻这么多切片，防止大库把一次运行拖爆
_MAX_EVIDENCE_LEN = 220         # 命中原文截多一点，审核时能看清上下文
_MAX_LLM_INSTANCES = 20         # 一次运行最多让模型读这么多个实例（花钱的闸）


@dataclass
class Proposal:
    instance_uid: str
    entity_type: str
    attribute: str
    value: Any
    method: str = "rule"
    confidence: float = 0.9
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        raw = f"{self.instance_uid}|{self.attribute}|{json.dumps(self.value, ensure_ascii=False, default=str)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------- 规则抽取 ----------------


def _cast(value: str, attr: AttributeDef) -> Any | None:
    """按属性类型转值；转不了就丢弃（宁可不抽，也不要写进一个错的类型）。"""
    text = (value or "").strip()
    if not text:
        return None
    if attr.type == "number":
        cleaned = text.replace(",", "")
        try:
            num = float(cleaned)
        except ValueError:
            return None
        return int(num) if num.is_integer() else num
    if attr.type == "enum":
        return text if text in (attr.values or ()) else None
    return text


def _snippet(content: str, start: int, end: int) -> str:
    lo = max(0, start - 60)
    hi = min(len(content), end + 60)
    return content[lo:hi].replace("\n", " ").strip()[:_MAX_EVIDENCE_LEN]


def _rule_match(content: str, attr: AttributeDef, name: str = "") -> tuple[Any, str] | None:
    """在文本里找这个属性的值。

    `pattern` 里可以写 `{name}`，运行时替换成实例名——表格类资料的写法是
    「KC | 流域蒸散发折算系数 | 敏感」，规则要能贴着实体的名字去找，否则一条规则
    只能复用一个实例。
    """
    rule = attr.extract or {}
    pattern = rule.get("pattern")
    if not pattern:
        return None
    if name:
        pattern = pattern.replace("{name}", re.escape(name))
    try:
        m = re.search(pattern, content)
    except re.error:
        logger.warning("属性 %s 的抽取正则非法：%s", attr.name, pattern)
        return None
    if not m:
        return None
    raw = m.group(1) if m.groups() else m.group(0)
    # 原文写法与枚举值不一定一致（「敏感」→「高」），按 map 翻译一手
    mapping = rule.get("map") or {}
    if isinstance(mapping, dict):
        raw = str(mapping.get(raw.strip(), raw))
    value = _cast(raw, attr)
    if value is None:
        return None
    return value, _snippet(content, m.start(), m.end())


async def _chunks_for(db: AsyncSession, name: str, kb_id: int | None) -> list[tuple[int, int, int | None, str, str]]:
    """按实例名找相关切片：返回 (chunk_id, doc_id, page, content, 文件名)。"""
    if not name or len(name) < 2:  # 单字名匹配噪音太大
        return []
    stmt = (
        select(Chunk.id, Chunk.doc_id, Chunk.page, Chunk.content, Document.filename)
        .join(Document, Chunk.doc_id == Document.id)
        .where(Chunk.content.ilike(f"%{name}%"))
        .limit(_MAX_CHUNKS_PER_INSTANCE)
    )
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    return [tuple(row) for row in (await db.execute(stmt)).all()]


async def run_rule_extraction(
    db: AsyncSession, reg: OntologyRegistry, *, kb_id: int | None = None
) -> list[Proposal]:
    """按 TBox 里的正则扫切片，给每个「实例 × 属性」抽一个值。"""
    rows = (
        await db.scalars(
            select(OntologyInstance).where(
                OntologyInstance.profile_id == reg.profile_id,
                OntologyInstance.status == "active",
            )
        )
    ).all()

    out: list[Proposal] = []
    for row in rows:
        entity: EntityTypeDef | None = reg.entity(row.entity_type)
        if entity is None:
            continue
        targets = [a for a in entity.attributes.values() if (a.extract or {}).get("pattern")]
        if not targets:
            continue
        name = str((row.props or {}).get("display_zh") or row.name)
        chunks = await _chunks_for(db, name, kb_id)
        if not chunks:
            continue
        for attr in targets:
            for chunk_id, doc_id, page, content, filename in chunks:
                hit = _rule_match(content or "", attr, name)
                if hit is None:
                    continue
                value, snippet = hit
                out.append(
                    Proposal(
                        instance_uid=row.uid,
                        entity_type=row.entity_type,
                        attribute=attr.name,
                        value=value,
                        method="rule",
                        confidence=0.9,
                        evidence={
                            "type": "自动抽取",
                            "note": f"正则命中：{attr.extract.get('pattern')}",
                            "chunk_id": chunk_id,
                            "doc_id": doc_id,
                            "doc_name": filename,
                            "page": page,
                            "snippet": snippet,
                        },
                    )
                )
                break  # 同一属性只取第一个命中，避免同一实例抽出一堆候选刷屏
    return out


# ---------------- LLM 抽取 ----------------

_LLM_SYSTEM = (
    "你是水利工程资料的信息抽取器。只依据给定原文抽取，**原文没有就返回 null**。"
    "严禁推测、严禁用常识补全、严禁编造数字。输出严格 JSON，不要解释。"
)


def _llm_prompt(name: str, entity_display: str, attrs: list[AttributeDef], chunks: list[str]) -> str:
    fields = "\n".join(
        f"- {a.name}（{a.type}）{a.description or ''}" for a in attrs
    )
    corpus = "\n\n".join(f"[片段{i + 1}] {c[:1200]}" for i, c in enumerate(chunks))
    return (
        f"实体：{name}（{entity_display}）\n"
        f"要抽取的字段：\n{fields}\n\n"
        f"原文：\n{corpus}\n\n"
        "请输出 JSON：{\"字段名\": {\"value\": 值或null, \"chunk\": 片段序号}}\n"
        "只填原文明确写到的字段；不确定的填 null。"
    )


def _parse_llm_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON（它有时会包 ```json 或加解释）。"""
    raw = text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def run_llm_extraction(
    db: AsyncSession,
    reg: OntologyRegistry,
    *,
    kb_id: int | None = None,
    skip: set[tuple[str, str]] | None = None,
) -> list[Proposal]:
    """把规则没抽到的实例交给模型读原文。`skip` = 已经有结论的 (instance_uid, attribute)。

    成本闸：一次最多读 _MAX_LLM_INSTANCES 个实例，每个实例只调一次模型。
    """
    from app.services.chat import build_chat_model

    skip = skip or set()
    rows = (
        await db.scalars(
            select(OntologyInstance).where(
                OntologyInstance.profile_id == reg.profile_id,
                OntologyInstance.status == "active",
            )
        )
    ).all()

    llm = None
    out: list[Proposal] = []
    used = 0
    for row in rows:
        entity = reg.entity(row.entity_type)
        if entity is None or not entity.attributes:
            continue
        pending = [a for a in entity.attributes.values() if (row.uid, a.name) not in skip]
        if not pending:
            continue
        name = str((row.props or {}).get("display_zh") or row.name)
        chunk_rows = await _chunks_for(db, name, kb_id)
        if not chunk_rows:
            continue
        if used >= _MAX_LLM_INSTANCES:
            break
        used += 1
        if llm is None:
            llm = build_chat_model(0.0)  # 抽取要稳定，温度 0
        texts = [c[3] or "" for c in chunk_rows[:3]]
        messages = [("system", _LLM_SYSTEM), ("human", _llm_prompt(name, entity.display_name, pending, texts))]
        try:
            resp = await llm.ainvoke(messages)
            content = getattr(resp, "content", "") or ""
        except Exception as exc:  # noqa: BLE001 - 单次失败不该毁掉整轮
            logger.warning("LLM 抽取失败（%s）：%s", name, exc)
            continue

        data = _parse_llm_json(content if isinstance(content, str) else str(content))
        by_name = {a.name: a for a in pending}
        for attr_name, payload in data.items():
            attr = by_name.get(attr_name)
            if attr is None or not isinstance(payload, dict):
                continue
            value = _cast(str(payload.get("value") or ""), attr)
            if value is None:
                continue
            idx = payload.get("chunk")
            chunk_idx = (int(idx) - 1) if isinstance(idx, (int, float)) and int(idx) > 0 else 0
            chunk = chunk_rows[min(chunk_idx, len(chunk_rows) - 1)]
            out.append(
                Proposal(
                    instance_uid=row.uid,
                    entity_type=row.entity_type,
                    attribute=attr_name,
                    value=value,
                    method="llm",
                    confidence=0.55,
                    evidence={
                        "type": "自动抽取",
                        "note": "大模型按原文抽取，需人工确认",
                        "chunk_id": chunk[0],
                        "doc_id": chunk[1],
                        "doc_name": chunk[4],
                        "page": chunk[2],
                        "snippet": (chunk[3] or "")[: _MAX_EVIDENCE_LEN].replace("\n", " "),
                    },
                )
            )
    return out


# ---------------- 落暂存区 ----------------


async def save_proposals(
    db: AsyncSession, profile_id: str, proposals: list[Proposal]
) -> int:
    """写入待审区。已存在同结论（同库 + dedupe_key）就跳过，不重复堆积。"""
    if not proposals:
        return 0
    existing = set(
        (
            await db.scalars(
                select(OntologyProposal.dedupe_key).where(
                    OntologyProposal.profile_id == profile_id
                )
            )
        ).all()
    )
    saved = 0
    for item in proposals:
        key = item.dedupe_key
        if key in existing:
            continue
        db.add(
            OntologyProposal(
                profile_id=profile_id,
                instance_uid=item.instance_uid,
                entity_type=item.entity_type,
                attribute=item.attribute,
                value=item.value,
                method=item.method,
                confidence=item.confidence,
                evidence=item.evidence,
                dedupe_key=key,
                status="pending",
            )
        )
        existing.add(key)
        saved += 1
    await db.commit()
    return saved


# ---------------- 审核：接受 / 驳回 ----------------


async def accept_proposal(db: AsyncSession, proposal: OntologyProposal) -> bool:
    """把提案写进实例属性 + 追加证据。返回是否写入成功（实例已不存在则 False）。

    证据是**并集**语义：同一条原文重复接受不会堆重复项，也不会覆盖人工写的证据。
    """
    from app.services.graph.ontology import instances as inst_mod

    row = await inst_mod.get_instance_by_uid(db, proposal.instance_uid)
    if row is None:
        return False
    props = dict(row.props or {})
    props[proposal.attribute] = proposal.value
    evidence = inst_mod.merge_evidence(row.evidence or [], [proposal.evidence])
    inst_mod.apply_changes(row, props=props, evidence=evidence, source="extracted")
    await db.commit()
    await db.refresh(row)
    return True
