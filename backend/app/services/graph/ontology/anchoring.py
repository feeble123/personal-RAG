"""P2 证据自动锚定：给**已有**实例找原文出处（不改值）。

和 F4「自动抽取」的区别：

| | F4 抽取 | P2 锚定 |
|---|---|---|
| 干什么 | 从原文读出一个**属性值** | 指出「这段原文讲的就是这个实例的这个属性」 |
| 接受后 | 写进实例属性（可能覆盖人工值） | **只挂证据**，值要不要采用由人另点 |

为什么要分开：实例是人梳理的（链条驱动），程序没有资格改值；但"这条实例的依据
在库里哪一页"是可以机械核对的——这正是程序该干的活。

**铁律：模型只许指认与抄写，不许撰写。**

1. 候选切片只能来自检索，模型看不到"自己记得"的东西；
2. 模型必须**逐字抄**出原文（`quote`），程序校验这句话确实在被指认的切片里；
   校验不过整条丢弃——**假出处比没出处更坏**；
3. 值也必须出现在那句 quote 里，否则丢掉值、只留证据。

结果一律进「待审区」（`ontology_proposals`，`method="anchor"`），
人点接受才写进实例，复用 F4 已有的审核流程。
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

METHOD_ANCHOR = "anchor"
_MAX_CANDIDATES = 6          # 每个实例最多翻几段原文
_MAX_CHUNK_CHARS = 1200      # 每段送进模型的字数上限
_MIN_QUOTE_CHARS = 6         # 引用太短没有校验意义
_DEFAULT_MAX_INSTANCES = 20  # 一次运行最多锚定多少个实例（花钱的闸）

# 拒绝原因 → 人话（写在返回结果里，用户知道为什么少了）
REJECT_LABELS = {
    "no_chunk": "引用的片段编号不存在",
    "quote_too_short": "引用的原文太短",
    "quote_not_in_chunk": "原文里找不到这句引用（模型写错了）",
    "bad_attribute": "字段名不在本体定义里",
}


@dataclass(frozen=True)
class Candidate:
    """一段候选原文（检索命中后从库里取全文，校验依据的是全文而不是检索摘要）。"""

    chunk_id: int
    doc_id: int
    doc_name: str
    page: int | None
    section: str | None
    content: str


@dataclass(frozen=True)
class AnchorHit:
    """一条通过校验的锚定结论。"""

    instance_uid: str
    entity_type: str
    attribute: str  # "" = 整条实例
    candidate: Candidate
    quote: str
    value: Any = None
    confidence: float = 0.7

    @property
    def dedupe_key(self) -> str:
        """按「实例 + 字段 + 引用原文」去重。

        不带 chunk_id：同一页常被切成几个重复切片（手册参数表就是），
        带上 chunk_id 会让同一条依据在待审区出现好几次。
        """
        raw = f"anchor|{self.instance_uid}|{self.attribute}|{_normalize(self.quote)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "type": "自动锚定",
            "note": "大模型按原文指认，引用已逐字校验",
            "attributes": [self.attribute] if self.attribute else [],
            "doc_id": self.candidate.doc_id,
            "chunk_id": self.candidate.chunk_id,
            "doc_name": self.candidate.doc_name,
            "page": self.candidate.page,
            "quote": self.quote,
            "snippet": self.quote[:220],
            "verified": True,
            "traceable": True,
            "resolved_by": "chunk",
        }


@dataclass
class AnchorRunResult:
    scanned: int = 0          # 扫了几个实例
    with_candidates: int = 0  # 有几个实例检索到了候选切片
    proposed: int = 0         # 通过逐字校验的结论
    rejected: int = 0         # 被校验挡下来的
    saved: int = 0            # 新进待审区的（去重后）
    reject_reasons: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    preview: list[dict[str, Any]] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "with_candidates": self.with_candidates,
            "proposed": self.proposed,
            "rejected": self.rejected,
            "saved": self.saved,
            "reject_reasons": {
                REJECT_LABELS.get(k, k): v for k, v in self.reject_reasons.items()
            },
            "notes": self.notes,
            "preview": self.preview,
        }


# ---------------- 候选切片 ----------------


async def _keyword_ids(
    db: AsyncSession,
    query: str,
    *,
    kb_id: int | None,
    doc_ids: list[int] | None,
    limit: int,
) -> list[int]:
    """原文里逐字出现实体名/中文名的切片。

    为什么要有这一路：向量检索在**跨库**（kb_id=None）时容易被无关文档挤掉——
    实测「自由水蓄水容量」在全库检索排上来的全是支模方案、洪水风险图。
    而参数名在参数表里往往是逐字出现的，精确子串命中比语义相似更可靠。

    名字常有一两个字的出入（手册写「自由水蓄水**容量**」，实例叫「自由水蓄水**能力**」），
    所以整名没命中时再去掉末尾 1~2 字按前缀找——中文复合词里这招很管用。
    """
    text = (query or "").strip()
    if len(text) < 3:  # 太短（如 sm）子串噪音太大，交给向量检索
        return []
    probes = [text]
    for cut in (1, 2):
        prefix = text[: len(text) - cut]
        if len(prefix) >= 4 and prefix != text:
            probes.append(prefix)
    for probe in probes:
        stmt = select(Chunk.id).where(Chunk.content.contains(probe))
        if doc_ids:
            stmt = stmt.where(Chunk.doc_id.in_(doc_ids))
        elif kb_id is not None:
            stmt = stmt.where(Chunk.kb_id == kb_id)
        ids = [int(i) for i in (await db.execute(stmt.limit(limit))).scalars().all()]
        if ids:
            return ids
    return []


async def candidate_chunks(
    db: AsyncSession,
    query: str,
    *,
    kb_id: int | None,
    top_k: int = _MAX_CANDIDATES,
    doc_ids: list[int] | None = None,
) -> list[Candidate]:
    """找候选切片：先精确命中，再用向量/混合检索补同义表述，最后取全文。

    `doc_ids`：实例已经有依据文档时优先在这一两份文档里找——
    比起在全库里大海捞针，这既是更强的前缀信号，也省一次跨库排序。

    取全文而不是用检索返回的摘要：逐字校验必须对着真实切片内容做，
    摘要被截断过，拿它校验会把正确引用误判成假的。
    """
    from app.services import rag

    ids: list[int] = []
    try:
        ids.extend(
            await _keyword_ids(db, query, kb_id=kb_id, doc_ids=doc_ids, limit=top_k)
        )
    except Exception as exc:  # noqa: BLE001 - 关键词这一路挂了不影响向量那路
        logger.warning("锚定关键词检索失败（%s）：%s", query, exc)

    try:
        if doc_ids:
            hits = await rag.retrieve(db, query, doc_ids=doc_ids, top_k=top_k)
        else:
            hits = await rag.retrieve(db, query, kb_id=kb_id, top_k=top_k)
        ids.extend(int(getattr(h, "chunk_id", 0) or 0) for h in hits)
    except Exception as exc:  # noqa: BLE001 - 检索失败只影响这一个实例
        logger.warning("锚定检索失败（%s）：%s", query, exc)

    # 去重保序 + 限条数（关键词命中排在前面：它是最强的信号）
    seen: set[int] = set()
    trimmed: list[int] = []
    for i in ids:
        if i > 0 and i not in seen:
            seen.add(i)
            trimmed.append(i)
    ids = trimmed[: top_k + 3]
    if not ids:
        return []
    rows = (
        await db.execute(
            select(
                Chunk.id, Chunk.doc_id, Chunk.page, Chunk.section, Chunk.content, Document.filename
            ).join(Document, Chunk.doc_id == Document.id).where(Chunk.id.in_(ids))
        )
    ).all()
    by_id = {
        int(r[0]): Candidate(
            chunk_id=int(r[0]),
            doc_id=int(r[1]),
            page=r[2],
            section=r[3],
            content=r[4] or "",
            doc_name=r[5] or "",
        )
        for r in rows
    }
    return [by_id[i] for i in ids if i in by_id]


# ---------------- 模型判定 ----------------


_LLM_SYSTEM = (
    "你是水利工程资料的证据核对员。任务：判断给定原文片段是否真的在讲某个本体实例，"
    "并把原文那句话**逐字抄**出来。只依据给出的片段，严禁推测、严禁补全、严禁改写标点。"
    "输出严格 JSON，不要解释。"
)


def build_prompt(name: str, entity: EntityTypeDef, candidates: list[Candidate]) -> str:
    fields = "\n".join(
        f"- {a.name}：{a.description or a.name}" for a in entity.attributes.values()
    )
    corpus = "\n\n".join(
        f"[片段{i + 1}]（{c.doc_name} 第{c.page or '?'}页）\n{c.content[:_MAX_CHUNK_CHARS]}"
        for i, c in enumerate(candidates)
    )
    return (
        f"实例：{name}（{entity.display_name or entity.name}）\n"
        f"它的字段（attribute 只能填这些，整条实例填 \"\"）：\n{fields}\n\n"
        f"原文片段：\n{corpus}\n\n"
        "请输出 JSON：{\"items\":[{\"chunk\":片段序号,\"attribute\":\"字段名或空串\","
        "\"quote\":\"原文中连续出现的一句话，逐字抄\",\"value\":\"原文里写到的该字段值，没有就 null\"}]}\n"
        "只在片段确实讲这个实例时才输出；quote 必须是片段里能找到的连续原文。"
    )


def _normalize(text: str) -> str:
    """比对用：去掉所有空白。中文 PDF 里换行/空格位置很不稳定。"""
    return re.sub(r"\s+", "", text or "")


def quote_supported(quote: str, content: str) -> bool:
    """引用是否逐字出现在原文里（忽略空白差异）。"""
    q = _normalize(quote)
    return len(q) >= _MIN_QUOTE_CHARS and q in _normalize(content)


def value_in_quote(value: Any, quote: str) -> bool:
    """值是否真的出现在那句引用里。5.0 与 5 视为同一个数。"""
    if value is None or value == "":
        return False
    q = _normalize(quote)
    text = _normalize(str(value))
    if text and text in q:
        return True
    if isinstance(value, float) and value.is_integer():
        return _normalize(str(int(value))) in q
    return False


def _cast_value(raw: Any, attr: AttributeDef | None) -> Any:
    """按属性类型收口模型给的值。收不了就返回 None（宁可不给值，也不写进错的类型）。"""
    if attr is None:
        return None
    text = str(raw).strip()
    if attr.type == "number":
        try:
            num = float(text.replace(",", ""))
        except ValueError:
            return None
        return int(num) if num.is_integer() else num
    if attr.type == "enum":
        return text if text in (attr.values or ()) else None
    return text


def parse_llm_items(text: str) -> list[dict[str, Any]]:
    """从模型输出里抠出 items（可能包 ```json 或带解释）。"""
    raw = (text or "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def build_hit(
    item: dict[str, Any],
    candidates: list[Candidate],
    *,
    instance_uid: str,
    entity_type: str,
    entity: EntityTypeDef,
    result: AnchorRunResult,
) -> AnchorHit | None:
    """把模型给的一条结论变成 AnchorHit；任何一项校验不过就丢弃并记原因。"""
    try:
        index = int(item.get("chunk") or 0) - 1
    except (TypeError, ValueError):
        index = -1
    if index < 0 or index >= len(candidates):
        result.reject("no_chunk")
        return None
    candidate = candidates[index]

    attribute = str(item.get("attribute") or "").strip()
    if attribute and entity.attributes and attribute not in entity.attributes:
        result.reject("bad_attribute")
        return None

    quote = str(item.get("quote") or "").strip()
    if len(_normalize(quote)) < _MIN_QUOTE_CHARS:
        result.reject("quote_too_short")
        return None
    if not quote_supported(quote, candidate.content):
        result.reject("quote_not_in_chunk")
        return None

    raw_value = item.get("value")
    value = raw_value if raw_value not in (None, "", "null") else None
    # 值必须在引用里出现过，否则宁可不给值——不能凭模型"记得"
    if value is not None and not value_in_quote(value, quote):
        value = None
    if value is not None:
        value = _cast_value(value, entity.attributes.get(attribute))

    return AnchorHit(
        instance_uid=instance_uid,
        entity_type=entity_type,
        attribute=attribute,
        candidate=candidate,
        quote=quote,
        value=value,
        confidence=0.8 if value is not None else 0.7,
    )


# ---------------- 落待审区 ----------------


async def save_hits(db: AsyncSession, profile_id: str, hits: list[AnchorHit]) -> int:
    """写进待审区。同一条结论（同实例+同字段+同切片+同引用）不重复堆积。"""
    if not hits:
        return 0
    keys = [
        k
        for (k,) in (
            await db.execute(
                select(OntologyProposal.dedupe_key).where(
                    OntologyProposal.profile_id == profile_id
                )
            )
        ).all()
    ]
    existing = set(keys)
    saved = 0
    for hit in hits:
        key = hit.dedupe_key
        if key in existing:
            continue
        db.add(
            OntologyProposal(
                profile_id=profile_id,
                instance_uid=hit.instance_uid,
                entity_type=hit.entity_type,
                attribute=hit.attribute,
                value=hit.value if hit.value is not None else "",
                method=METHOD_ANCHOR,
                confidence=hit.confidence,
                evidence=hit.as_evidence(),
                dedupe_key=key,
                status="pending",
            )
        )
        existing.add(key)
        saved += 1
    await db.flush()
    return saved


# ---------------- 运行 ----------------


async def _call_llm(llm: Any, prompt: str) -> list[dict[str, Any]]:
    messages = [("system", _LLM_SYSTEM), ("human", prompt)]
    resp = await llm.ainvoke(messages)
    content = getattr(resp, "content", "") or ""
    return parse_llm_items(content if isinstance(content, str) else str(content))


async def run_anchoring(
    db: AsyncSession,
    reg: OntologyRegistry,
    *,
    kb_id: int | None = None,
    instance_uids: list[str] | None = None,
    max_instances: int = _DEFAULT_MAX_INSTANCES,
    top_k: int = _MAX_CANDIDATES,
    llm: Any = None,
) -> tuple[list[AnchorHit], AnchorRunResult]:
    """给当前库里已有实例找原文依据。不写实例，只返回结论（由调用方决定是否落待审区）。"""
    result = AnchorRunResult()
    stmt = select(OntologyInstance).where(
        OntologyInstance.profile_id == reg.profile_id,
        OntologyInstance.status == "active",
    )
    if instance_uids:
        stmt = stmt.where(OntologyInstance.uid.in_(instance_uids))
    rows = list((await db.scalars(stmt)).all())

    hits: list[AnchorHit] = []
    for row in rows:
        if result.scanned >= max_instances:
            result.notes.append(f"实例太多，本次只处理前 {max_instances} 个，剩下的再跑一次")
            break
        entity = reg.entity(row.entity_type)
        if entity is None:
            continue
        result.scanned += 1
        name = str((row.props or {}).get("display_zh") or row.name)
        # 没指定知识库就用实例自己的库（0 = 全局实例 → 全库查找）
        scope_kb = kb_id if kb_id is not None else (row.kb_id or None)
        # 实例已经写明依据文档的，优先在那一两份文档里找（比全库大海捞针准得多）
        doc_ids = [
            int(e["doc_id"])
            for e in (row.evidence or [])
            if isinstance(e, dict) and e.get("doc_id") is not None
        ]
        candidates = await candidate_chunks(
            db, name, kb_id=scope_kb, top_k=top_k, doc_ids=doc_ids or None
        )
        if not candidates:
            result.notes.append(f"{name}：知识库里没检索到相关片段")
            continue
        result.with_candidates += 1
        if llm is None:
            from app.services.chat import build_chat_model

            llm = build_chat_model(0.0)  # 判定要稳定，温度 0
        try:
            items = await _call_llm(llm, build_prompt(name, entity, candidates))
        except Exception as exc:  # noqa: BLE001 - 单个实例失败不该毁掉整轮
            logger.warning("锚定判定失败（%s）：%s", name, exc)
            result.notes.append(f"{name}：模型判定失败（{exc.__class__.__name__}）")
            continue
        for item in items:
            hit = build_hit(
                item,
                candidates,
                instance_uid=row.uid,
                entity_type=row.entity_type,
                entity=entity,
                result=result,
            )
            if hit is None:
                continue
            result.proposed += 1
            hits.append(hit)
            result.preview.append(
                {
                    "instance": name,
                    "attribute": hit.attribute,
                    "value": hit.value,
                    "doc_name": hit.candidate.doc_name,
                    "page": hit.candidate.page,
                    "quote": hit.quote[:160],
                }
            )
    return hits, result
