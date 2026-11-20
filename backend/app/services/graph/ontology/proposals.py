"""F5 图提案：让模型提出「这里有哪些新实例、它们之间什么关系」。

和已有两条链路的分工
--------------------
| | F4 抽取 | P2 锚定 | **F5 提案（本模块）** |
|---|---|---|---|
| 对象 | 已有实例的字段 | 已有实例的出处 | **新实例 + 新关系** |
| 接受后 | 写字段 | 挂证据 | **建实例、建边** |

F4/P2 只能给**已经建好**的实例补内容。可本体最费工的是把实例和关系从原文里
梳理出来——88 个实例、92 条关系全是人一条条建的。本模块补的就是这一段：
模型读原文提候选，人只审不建。

**铁律**（比 anchoring 更严，因为它造的是新东西而不是给旧东西做注）

1. 只许依据给定切片，`quote` 必须逐字抄——假出处比没出处更坏；
2. 类型、关系只能用 TBox 里有的标识，**模型自创的概念一律丢弃**；
3. **端点必须对**：`RESPONDS_WITH` 只能 `response_level → measure`，写反了直接拒。
   这条正是人工建模时最容易犯的错（我们自己就被校验器抓出过 5 处）；
4. 关系两端必须解析到「库里已有实例」或「本批同时声明的新实例」，不许悬空；
5. 已有同名实例不新建，标成疑似重复交给人判断。

**提案可以有缺口，落地不行**：缺必填字段只记 warning、照样进待审区（缺什么由
人补），但真正写进库时（accept）会再查一次。这一条和体检面板是一套标准。

**一条刻意不加的校验**（有实测依据，别"顺手加固"）

有人会想：关系的引句里总该出现两端的名字吧？**不能这么查。** 拿我们自己的
92 条人工核实过的关系量过：两端名字都在引句里的 **0 条**，一个都不在的
**72 条**。中文技术文档是「先给实体、再在正文里说规则」，证据句承载的是关系的
**内容**（泄量、阈值、触发条件），不重复点名——比如
`2~3年一遇降雨 --STIPULATES--> 2~3年一遇控泄方案` 的证据是「洪水控制下泄流量70m³/s」。

硬要求名字出现，等于拒掉 78% 的正确数据，还会逼模型去编一句同时含着两个名字
的假引文——比不加校验更坏。**一条边连得对不对，机器判不了**，所以它只能是一
条提案：类型对不对、端点搭不搭、引句真不真，机器查；连得对不对，人看。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyProposal
from app.services.graph.ontology import validator
from app.services.graph.ontology.anchoring import Candidate, _normalize, quote_supported, value_in_quote
from app.services.graph.ontology.registry import AttributeDef, OntologyRegistry

logger = logging.getLogger(__name__)

KIND_INSTANCE = "instance"
KIND_RELATION = "relation"
METHOD_GRAPH = "graph"

_MAX_CHUNK_CHARS = 1200    # 每段送进模型的字数上限
_MAX_CATALOG = 120         # prompt 里最多列这么多个已有实例名
_MAX_NOTES = 60            # 返回给人看的备注上限，防止一次拒太多刷屏
_MIN_QUOTE_CHARS = 6       # 引用太短没有校验意义

# 拒绝原因 → 人话（写在返回结果里，用户知道为什么少了）
REJECT_LABELS = {
    "unknown_type": "类型不在本体定义里",
    "unknown_relation": "关系不在本体定义里",
    "no_name": "实例没给名字",
    "duplicate_instance": "库里已有同名实例，不重复新建",
    "list_name": "实例名是一串并列的东西，不是单个实体",
    "no_chunk": "引用的片段编号不存在",
    "quote_too_short": "引用的原文太短",
    "quote_not_in_chunk": "原文里找不到这句引用（模型写错了）",
    "unknown_ref": "关系提到了没有给出的实例",
    "ref_rejected": "连到的实例本身没通过校验",
    "self_relation": "关系的两头是同一个实例",
    "reversed_endpoint": "关系写反了（主语宾语调换才对）",
    "bad_endpoint": "关系两端的类型不允许",
}


# ---------------- 数据结构 ----------------


@dataclass(frozen=True)
class Known:
    """提案里可以连过去的一个实例。

    `origin` 区分它是**库里早就有**的还是**本次运行刚提的**——重复提案的提示
    要说准是哪种，否则人看到"库里已经有了"会去库里翻，翻不到。
    """

    uid: str
    entity_type: str
    name: str
    origin: str = "db"


# 重复提案的提示语：按来源说准，别让人去库里找一个还没入库的东西
_DUP_WHERE = {"db": "库里已经有了", "run": "本次运行已经提过了"}


@dataclass(frozen=True)
class InstanceCandidate:
    """一条通过校验的新实例提案。"""

    entity_type: str
    name: str
    uid: str
    props: dict[str, Any]
    candidate: Candidate
    quote: str
    #: uid 是按库生成的（库内类型的 uid 里编了 kb_id），落地时必须按同一个库写，
    #: 否则 uid 说属于 A 库、行记录说属于 0 库，之后按 uid 查就查不到了。
    kb_id: int = 0
    warnings: tuple[str, ...] = ()
    confidence: float = 0.6

    @property
    def dedupe_key(self) -> str:
        return _sha1(f"instance|{self.uid}")

    def payload(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "name": self.name,
            "uid": self.uid,
            "props": dict(self.props),
            "kb_id": self.kb_id,
        }

    def evidence(self) -> dict[str, Any]:
        return _evidence(self.candidate, self.quote)


@dataclass(frozen=True)
class RelationCandidate:
    """一条通过校验的新关系提案。"""

    subject_uid: str
    predicate: str
    object_uid: str
    subject_name: str
    object_name: str
    candidate: Candidate
    quote: str
    props: dict[str, Any] = field(default_factory=dict)
    kb_id: int = 0
    confidence: float = 0.6

    @property
    def dedupe_key(self) -> str:
        """按「主语 + 关系 + 宾语」去重。

        **刻意不带 quote**：同一条事实在两段原文里都写了，是同一件事，待审区
        不该出现两遍（证据合并是 accept 那边的事）。
        """
        return _sha1(f"relation|{self.subject_uid}|{self.predicate}|{self.object_uid}")

    def payload(self) -> dict[str, Any]:
        return {
            "subject_uid": self.subject_uid,
            "predicate": self.predicate,
            "object_uid": self.object_uid,
            "props": dict(self.props),
            "kb_id": self.kb_id,
        }

    def evidence(self) -> dict[str, Any]:
        return _evidence(self.candidate, self.quote)


@dataclass
class ProposalRunResult:
    """一次提案运行的结果——给界面看的账本，说明白"少了的东西去哪了"。"""

    chunks: int = 0      # 范围内一共有多少段可扫
    scanned: int = 0     # 真正送进模型的有几段（**没送的就是没扫**）
    instances: int = 0
    relations: int = 0
    rejected: int = 0
    saved: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    preview: list[dict[str, Any]] = field(default_factory=list)

    def reject(self, reason: str, detail: str = "") -> None:
        self.rejected += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
        if detail and len(self.notes) < _MAX_NOTES:
            self.notes.append(detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "scanned": self.scanned,
            "instances": self.instances,
            "relations": self.relations,
            "rejected": self.rejected,
            "saved": self.saved,
            "reject_reasons": {
                REJECT_LABELS.get(k, k): v for k, v in self.reject_reasons.items()
            },
            "notes": self.notes,
            "preview": self.preview,
        }


def _sha1(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _evidence(candidate: Candidate, quote: str) -> dict[str, Any]:
    """证据只记「文件 + 页码 + 逐字引句」——和 anchoring / 数据文件同一套。"""
    return {
        "type": "图提案",
        "note": "大模型按原文提出，引用已逐字校验",
        "doc_id": candidate.doc_id,
        "chunk_id": candidate.chunk_id,
        "doc_name": candidate.doc_name,
        "page": candidate.page,
        "printed_page": candidate.printed_page,
        "quote": quote,
        "snippet": quote[:220],
        "verified": True,
        "traceable": True,
        "resolved_by": "chunk",
    }


# ---------------- 已有实例索引 ----------------


def known_from_instances(
    rows: Iterable[Any],
) -> tuple[dict[str, Known], dict[str, Known], set[str]]:
    """已有实例 → (按名字, 按 uid, 重名集合)。

    两条容易踩的坑，都是实测踩出来的：

    - **只按 `display_zh` 建索引会漏**：模型和人都可能用实例的原始名，两个都
      得认，所以每个实例同时挂中文名和原名两个键。
    - **同名不同实例不能猜**：两个实例标签一样时，随便挑一个就是"连错边"——
      比连不上更坏。这种名字**整条作废**（放进 `ambiguous`），让调用方去提示人。
    """
    by_name: dict[str, Known] = {}
    by_uid: dict[str, Known] = {}
    ambiguous: set[str] = set()
    for row in rows:
        props = getattr(row, "props", None) or {}
        display = _clean_name(str(props.get("display_zh") or ""))
        raw = _clean_name(str(getattr(row, "name", "") or ""))
        item = Known(
            uid=str(row.uid),
            entity_type=str(row.entity_type),
            name=display or raw,
        )
        by_uid[item.uid] = item
        for key in (display, raw):
            if not key:
                continue
            seen = by_name.get(key)
            if seen is not None and seen.uid != item.uid:
                ambiguous.add(key)
                continue
            by_name.setdefault(key, item)
    for key in ambiguous:
        by_name.pop(key, None)
    return by_name, by_uid, ambiguous


# ---------------- 逐条硬校验 ----------------


_WRAP = "「」『』“”\"'《》【】[]（）() \t\r\n"

# 实例名里出现这些，多半是把一串实体塞成了一个。**只认逗号顿号**——
# 「和」「及」在正常地名里也有（和平镇），拿来判断会误伤。
_LIST_SEPARATORS = ("、", "，", "；", ";", ",")


def _clean_name(text: str) -> str:
    """去掉模型爱加的引号书名号和首尾空白。**只清洗，不做模糊匹配**——
    名字对不上就是不认识，宁可少连一条边，也不要连错。"""
    return (text or "").strip().strip(_WRAP).strip()


def _pick_candidate(
    item: dict[str, Any], chunks: list[Candidate], result: ProposalRunResult
) -> Candidate | None:
    try:
        index = int(item.get("chunk") or 0) - 1
    except (TypeError, ValueError):
        index = -1
    if index < 0 or index >= len(chunks):
        result.reject("no_chunk", f"片段序号 {item.get('chunk')!r} 不存在")
        return None
    return chunks[index]


def _quote_of(
    item: dict[str, Any], chunk: Candidate, result: ProposalRunResult
) -> str | None:
    quote = str(item.get("quote") or "").strip()
    if len(_normalize(quote)) < _MIN_QUOTE_CHARS:
        result.reject("quote_too_short", f"引用太短：{quote!r}")
        return None
    if not quote_supported(quote, chunk.content):
        result.reject("quote_not_in_chunk", f"原文里找不到这句：{quote[:40]!r}")
        return None
    return quote


def _coerce(value: Any, attr: AttributeDef) -> tuple[bool, Any]:
    """按 TBox 声明的类型收口。收不住就 (False, None)——**不猜**。"""
    if value is None or value == "":
        return False, None
    if attr.type == "number":
        if isinstance(value, bool):
            return False, None
        try:
            num = float(str(value).replace(",", "").replace("，", ""))
        except ValueError:
            return False, None
        return True, int(num) if num.is_integer() else num
    if attr.type == "boolean":
        text = str(value).strip().lower()
        if text in ("true", "1", "是", "yes"):
            return True, True
        if text in ("false", "0", "否", "no"):
            return True, False
        return False, None
    if attr.type == "enum":
        text = str(value).strip()
        return (True, text) if text in (attr.values or ()) else (False, None)
    if attr.type in ("list", "range"):
        if not isinstance(value, (list, tuple)):
            return False, None
        if attr.type == "range" and len(value) != 2:
            return False, None
        return True, list(value)
    if isinstance(value, (dict, list)):
        return False, None
    return True, str(value).strip()


def _all_in_quote(value: Any, quote: str) -> bool:
    if isinstance(value, (list, tuple)):
        return all(value_in_quote(v, quote) for v in value)
    return value_in_quote(value, quote)


def _is_source_name(value: Any, doc_name: str | None) -> bool:
    """值是不是就是这段原文出自的那份文档名。

    `standard_clause.doc` 这类字段填的就是文件名，而引句是条款正文、永远不含
    文件名——**这个值不需要靠引句证明**：切片来自哪份文档是系统确知的事实，
    不是模型的一次断言。实测真实数据 9 个条款实例全部这么填，不加这条例外，
    每个条款都会莫名其妙少一个字段。
    """
    if not doc_name or not isinstance(value, str):
        return False
    left = value.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    right = doc_name.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return bool(left) and left == right


def _coerce_props(
    registry: OntologyRegistry,
    entity_type: str,
    raw: Any,
    quote: str,
    doc_name: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """只留「TBox 里有定义、类型收得住、值确实出现在引句里」的字段。

    宁可少一个字段，也不要一个点不回原文的值——那正是脏数据的来源。
    缺必填字段只记 warning（提案可以不全，落地时才拦）。
    """
    ent = registry.entity(entity_type)
    props: dict[str, Any] = {}
    warnings: list[str] = []
    if not ent or not isinstance(raw, dict):
        return props, warnings
    for key, value in raw.items():
        attr = ent.attributes.get(str(key))
        if attr is None:
            warnings.append(f"字段 {key} 不在本体定义里，已丢")
            continue
        ok, coerced = _coerce(value, attr)
        if not ok:
            warnings.append(f"字段 {key} 的值 {value!r} 类型不对，已丢")
            continue
        if not _all_in_quote(coerced, quote) and not _is_source_name(coerced, doc_name):
            warnings.append(f"字段 {key} 的值 {coerced!r} 在引句里找不到，已丢")
            continue
        props[str(key)] = coerced
    for name in validator.missing_required(registry, entity_type, props):
        warnings.append(f"缺必填字段 {name}")
    return props, warnings


def _build_instance(
    item: dict[str, Any],
    chunks: list[Candidate],
    *,
    registry: OntologyRegistry,
    by_name: dict[str, Known],
    by_uid: dict[str, Known],
    kb_id: int,
    result: ProposalRunResult,
) -> InstanceCandidate | None:
    entity_type = str(item.get("type") or "").strip()
    if registry.entity(entity_type) is None:
        result.reject("unknown_type", f"类型 {entity_type or '(空)'} 不在本体定义里")
        return None
    name = _clean_name(str(item.get("name") or ""))
    if not name:
        result.reject("no_name", f"{entity_type} 有一条没给名字")
        return None
    if any(sep in name for sep in _LIST_SEPARATORS):
        # 实测真实模型会把「镇人民政府、街道办事处、区政府各部门」当成**一个**责任
        # 单位。名字里出现顿号基本就是它把一串东西塞进来了——建出来的是一个
        # 查不到、也挂不上关系的假实体。宁可拒掉让人自己拆。
        result.reject("list_name", f"「{name}」像一串并列的实体，不是一个")
        return None
    uid = registry.uid_for(entity_type, name, kb_id)
    dup = by_name.get(name) or by_uid.get(uid)
    if dup is not None:
        where = _DUP_WHERE.get(dup.origin, _DUP_WHERE["db"])
        result.reject("duplicate_instance", f"{name}：{where}，不重复新建")
        return None
    chunk = _pick_candidate(item, chunks, result)
    if chunk is None:
        return None
    quote = _quote_of(item, chunk, result)
    if quote is None:
        return None
    props, warnings = _coerce_props(
        registry, entity_type, item.get("props"), quote, chunk.doc_name
    )
    return InstanceCandidate(
        entity_type=entity_type,
        name=name,
        uid=uid,
        props=props,
        candidate=chunk,
        quote=quote,
        kb_id=kb_id,
        warnings=tuple(warnings),
    )


def _resolve(ref: Any, by_name: dict[str, Known], by_uid: dict[str, Known]) -> Known | None:
    text = _clean_name(str(ref or ""))
    if not text:
        return None
    if text in by_uid:
        return by_uid[text]
    return by_name.get(text)


def _build_relation(
    item: dict[str, Any],
    chunks: list[Candidate],
    *,
    registry: OntologyRegistry,
    by_name: dict[str, Known],
    by_uid: dict[str, Known],
    kb_id: int,
    result: ProposalRunResult,
    rejected_names: set[str] | None = None,
) -> RelationCandidate | None:
    predicate = str(item.get("predicate") or "").strip()
    if registry.relation(predicate) is None:
        result.reject("unknown_relation", f"关系 {predicate or '(空)'} 不在本体定义里")
        return None
    subject = _resolve(item.get("subject"), by_name, by_uid)
    obj = _resolve(item.get("object"), by_name, by_uid)
    if subject is None or obj is None:
        raw = item.get("subject") if subject is None else item.get("object")
        missing = _clean_name(str(raw or ""))
        # 一个实例没通过校验，引用它的每条边都会「找不到」——那是同一个根因。
        # 分开报会让人以为模型编了好几个不存在的东西。
        if rejected_names and missing in rejected_names:
            result.reject(
                "ref_rejected",
                f"{predicate} 连到的「{missing}」自己没通过校验，这条关系跟着作废",
            )
        else:
            result.reject("unknown_ref", f"{predicate} 提到了没给出的「{missing}」")
        return None
    if subject.uid == obj.uid:
        result.reject("self_relation", f"{predicate} 两头都是 {subject.name}")
        return None
    if not registry.endpoints_ok(predicate, subject.entity_type, obj.entity_type):
        # 端点反了是最常见的错——单独报出来，人一眼能看懂该改哪头
        if registry.endpoints_ok(predicate, obj.entity_type, subject.entity_type):
            result.reject("reversed_endpoint", f"{predicate} 写反了：{subject.name} ↔ {obj.name}")
        else:
            result.reject(
                "bad_endpoint",
                f"{predicate} 不能连 {subject.entity_type} → {obj.entity_type}",
            )
        return None
    chunk = _pick_candidate(item, chunks, result)
    if chunk is None:
        return None
    quote = _quote_of(item, chunk, result)
    if quote is None:
        return None
    props = item.get("props") if isinstance(item.get("props"), dict) else {}
    return RelationCandidate(
        subject_uid=subject.uid,
        predicate=predicate,
        object_uid=obj.uid,
        subject_name=subject.name,
        object_name=obj.name,
        candidate=chunk,
        quote=quote,
        props=dict(props),
        kb_id=kb_id,
    )


def build_candidates(
    instance_items: list[dict[str, Any]],
    relation_items: list[dict[str, Any]],
    chunks: list[Candidate],
    *,
    registry: OntologyRegistry,
    by_name: dict[str, Known],
    by_uid: dict[str, Known],
    kb_id: int = 0,
    result: ProposalRunResult | None = None,
) -> tuple[list[InstanceCandidate], list[RelationCandidate]]:
    """把模型给的 items 逐条过校验，能过的才变成候选。

    **先实例后关系**是硬顺序：关系要能连到「本批新提的实例」，所以实例先全部
    校验完，把能过的那批并进查找表，关系才开始解析。库里已有实例优先——同名
    时连到已有的，不会连到提案。
    """
    result = result or ProposalRunResult()

    # 已有实例优先：同名冲突时 scope 里保留已有的那个
    scope_name = dict(by_name)
    scope_uid = dict(by_uid)

    instances: list[InstanceCandidate] = []
    seen: set[str] = set()
    rejected_names: set[str] = set()
    for item in instance_items:
        cand = _build_instance(
            item,
            chunks,
            registry=registry,
            by_name=scope_name,
            by_uid=scope_uid,
            kb_id=kb_id,
            result=result,
        )
        if cand is None:
            name = _clean_name(str(item.get("name") or ""))
            if name:
                rejected_names.add(name)
            continue
        if cand.dedupe_key in seen:
            continue
        seen.add(cand.dedupe_key)
        instances.append(cand)
        known = Known(uid=cand.uid, entity_type=cand.entity_type, name=cand.name)
        scope_uid[cand.uid] = known
        scope_name.setdefault(_clean_name(cand.name), known)

    relations: list[RelationCandidate] = []
    seen_rel: set[str] = set()
    for item in relation_items:
        cand = _build_relation(
            item,
            chunks,
            registry=registry,
            by_name=scope_name,
            by_uid=scope_uid,
            kb_id=kb_id,
            result=result,
            rejected_names=rejected_names,
        )
        if cand is None or cand.dedupe_key in seen_rel:
            continue
        seen_rel.add(cand.dedupe_key)
        relations.append(cand)

    result.instances += len(instances)
    result.relations += len(relations)
    result.preview = [
        {"kind": KIND_INSTANCE, "name": c.name, "type": c.entity_type,
         "doc_name": c.candidate.doc_name, "quote": c.quote[:160], "warnings": list(c.warnings)}
        for c in instances
    ] + [
        {"kind": KIND_RELATION, "name": f"{c.subject_name} —{c.predicate}→ {c.object_name}",
         "type": c.predicate, "doc_name": c.candidate.doc_name,
         "quote": c.quote[:160], "warnings": []}
        for c in relations
    ]
    return instances, relations


# ---------------- 落待审区 ----------------


def _add(
    db: AsyncSession,
    profile_id: str,
    existing: set[str],
    key: str,
    fields: dict[str, Any],
) -> int:
    if key in existing:
        return 0
    db.add(OntologyProposal(profile_id=profile_id, dedupe_key=key, status="pending", **fields))
    existing.add(key)
    return 1


async def save_proposals(
    db: AsyncSession,
    profile_id: str,
    instances: list[InstanceCandidate],
    relations: list[RelationCandidate],
) -> int:
    """写进待审区。同一条事实反复跑不会重复堆积。

    不在这里落库：调用方（C-2/C-3 的运行入口）决定何时 commit，和 F4/P2 一致。
    """
    if not instances and not relations:
        return 0
    existing = set(
        (await db.execute(
            select(OntologyProposal.dedupe_key).where(OntologyProposal.profile_id == profile_id)
        )).scalars().all()
    )
    saved = 0
    for cand in instances:
        saved += _add(
            db, profile_id, existing, cand.dedupe_key,
            {
                "instance_uid": cand.uid,
                "entity_type": cand.entity_type,
                "attribute": "",
                "value": cand.payload(),
                "kind": KIND_INSTANCE,
                "payload": cand.payload(),
                "method": METHOD_GRAPH,
                "confidence": cand.confidence,
                "evidence": cand.evidence(),
            },
        )
    for cand in relations:
        saved += _add(
            db, profile_id, existing, cand.dedupe_key,
            {
                "instance_uid": cand.subject_uid,
                "entity_type": "",
                "attribute": "",
                "value": cand.payload(),
                "kind": KIND_RELATION,
                "payload": cand.payload(),
                "method": METHOD_GRAPH,
                "confidence": cand.confidence,
                "evidence": cand.evidence(),
            },
        )
    await db.flush()
    return saved
