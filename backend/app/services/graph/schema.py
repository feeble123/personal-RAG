"""防洪知识图谱：实体/关系字典与数据结构（第一期）。

本模块是图谱的「骨架」——只定义**允许出现的实体类型、关系类型、端点合法性**，
抽取规则、台账导入、图存储、检索融合都以此为准。改骨架 = 改这里 + 前端常量。

与后端现有 `app/core/taxonomy.py`（业务域/文档类型）分工：
- taxonomy：文档属于哪个业务域、什么类型（库层面）
- 本文件：文档里的**事实**由哪些实体和关系构成（图层面）
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# 用 (str, Enum) 而非 StrEnum：本机测试环境是 Python 3.10，StrEnum 要 3.11+
class EntityType(str, Enum):
    """11 类实体。前 9 类与用户确认的通用清单一致，后 2 类是防洪链的起点和终点。"""

    ENGINEERING_OBJECT = "engineering_object"  # 工程对象：水库/大坝/溢洪道/闸门
    RIVER = "river"  # 河流河段（承载「安全泄量」）
    ADMIN_REGION = "admin_region"  # 行政区：镇/村
    STANDARD_CLAUSE = "standard_clause"  # 规范条款
    METRIC = "metric"  # 指标定义（数值放主体节点属性）
    MANAGEMENT_UNIT = "management_unit"  # 管理单位 / 责任岗位
    RESPONSE_LEVEL = "response_level"  # 预案响应等级
    EVENT = "event"  # 事件/险情
    TERM = "term"  # 专业术语
    CONDITION = "condition"  # 工况：设计/校核/汛限
    MEASURE = "measure"  # 措施：预警发布/转移安置


class RelationType(str, Enum):
    """10 种关系，只保留防洪链上用得着的。"""

    STIPULATES = "stipulates"  # 规定：工况 → 河段/指标（下泄流量、安全泄量）
    DOWNSTREAM_OF = "downstream_of"  # 下游
    LOCATED_IN = "located_in"  # 位于
    NEEDS_WARNING = "needs_warning"  # 需预警：行政区 → 措施
    NEEDS_EVACUATION = "needs_evacuation"  # 需转移：行政区 → 措施
    BASED_ON = "based_on"  # 依据：事实 → 条款
    RESPONSIBLE = "responsible"  # 责任：措施/工程 → 单位
    PART_OF = "part_of"  # 属于：建筑物 → 水库
    FLOWS_INTO = "flows_into"  # 汇入：河段 → 河段
    HAS_PLAN = "has_plan"  # 对应预案：行政区 → 响应等级


# Neo4j 标签/关系类型（大写驼峰 / 大写蛇形），与 Python 值一一对应
ENTITY_LABELS: dict[EntityType, str] = {
    EntityType.ENGINEERING_OBJECT: "EngineeringObject",
    EntityType.RIVER: "River",
    EntityType.ADMIN_REGION: "AdminRegion",
    EntityType.STANDARD_CLAUSE: "StandardClause",
    EntityType.METRIC: "Metric",
    EntityType.MANAGEMENT_UNIT: "ManagementUnit",
    EntityType.RESPONSE_LEVEL: "ResponseLevel",
    EntityType.EVENT: "Event",
    EntityType.TERM: "Term",
    EntityType.CONDITION: "Condition",
    EntityType.MEASURE: "Measure",
}

ENTITY_LABELS_ZH: dict[EntityType, str] = {
    EntityType.ENGINEERING_OBJECT: "工程对象",
    EntityType.RIVER: "河流河段",
    EntityType.ADMIN_REGION: "行政区",
    EntityType.STANDARD_CLAUSE: "规范条款",
    EntityType.METRIC: "指标",
    EntityType.MANAGEMENT_UNIT: "管理单位",
    EntityType.RESPONSE_LEVEL: "响应等级",
    EntityType.EVENT: "事件险情",
    EntityType.TERM: "专业术语",
    EntityType.CONDITION: "工况",
    EntityType.MEASURE: "措施",
}

RELATION_LABELS: dict[RelationType, str] = {
    RelationType.STIPULATES: "STIPULATES",
    RelationType.DOWNSTREAM_OF: "DOWNSTREAM_OF",
    RelationType.LOCATED_IN: "LOCATED_IN",
    RelationType.NEEDS_WARNING: "NEEDS_WARNING",
    RelationType.NEEDS_EVACUATION: "NEEDS_EVACUATION",
    RelationType.BASED_ON: "BASED_ON",
    RelationType.RESPONSIBLE: "RESPONSIBLE",
    RelationType.PART_OF: "PART_OF",
    RelationType.FLOWS_INTO: "FLOWS_INTO",
    RelationType.HAS_PLAN: "HAS_PLAN",
}

RELATION_LABELS_ZH: dict[RelationType, str] = {
    RelationType.STIPULATES: "规定",
    RelationType.DOWNSTREAM_OF: "下游",
    RelationType.LOCATED_IN: "位于",
    RelationType.NEEDS_WARNING: "需预警",
    RelationType.NEEDS_EVACUATION: "需转移",
    RelationType.BASED_ON: "依据",
    RelationType.RESPONSIBLE: "责任",
    RelationType.PART_OF: "属于",
    RelationType.FLOWS_INTO: "汇入",
    RelationType.HAS_PLAN: "对应预案",
}

# 端点合法性：subject_type → 允许的 object_type 集合。
# 防止 LLM/规则抽出「条款位于行政区」这类无意义边。
RELATION_ENDPOINTS: dict[RelationType, dict[EntityType, set[EntityType]]] = {
    RelationType.STIPULATES: {
        EntityType.CONDITION: {EntityType.RIVER, EntityType.METRIC},
        EntityType.STANDARD_CLAUSE: {EntityType.METRIC},
    },
    RelationType.DOWNSTREAM_OF: {
        EntityType.ENGINEERING_OBJECT: {
            EntityType.ADMIN_REGION,
            EntityType.ENGINEERING_OBJECT,
            EntityType.RIVER,
        },
        EntityType.RIVER: {EntityType.RIVER, EntityType.ADMIN_REGION},
    },
    RelationType.LOCATED_IN: {
        EntityType.ENGINEERING_OBJECT: {EntityType.ADMIN_REGION},
        EntityType.ADMIN_REGION: {EntityType.ADMIN_REGION},
    },
    RelationType.NEEDS_WARNING: {EntityType.ADMIN_REGION: {EntityType.MEASURE}},
    RelationType.NEEDS_EVACUATION: {EntityType.ADMIN_REGION: {EntityType.MEASURE}},
    RelationType.BASED_ON: {
        EntityType.MEASURE: {EntityType.STANDARD_CLAUSE},
        EntityType.CONDITION: {EntityType.STANDARD_CLAUSE},
        EntityType.RIVER: {EntityType.STANDARD_CLAUSE},
        EntityType.EVENT: {EntityType.STANDARD_CLAUSE},
        EntityType.ENGINEERING_OBJECT: {EntityType.STANDARD_CLAUSE},
    },
    RelationType.RESPONSIBLE: {
        EntityType.MEASURE: {EntityType.MANAGEMENT_UNIT},
        EntityType.ENGINEERING_OBJECT: {EntityType.MANAGEMENT_UNIT},
    },
    RelationType.PART_OF: {EntityType.ENGINEERING_OBJECT: {EntityType.ENGINEERING_OBJECT}},
    RelationType.FLOWS_INTO: {EntityType.RIVER: {EntityType.RIVER}},
    RelationType.HAS_PLAN: {EntityType.ADMIN_REGION: {EntityType.RESPONSE_LEVEL}},
}

# 全局实体（跨库共享）vs 库内私有实体（同名不同库不串）
GLOBAL_ENTITY_TYPES: frozenset[EntityType] = frozenset(
    {
        EntityType.ADMIN_REGION,
        EntityType.RIVER,
        EntityType.STANDARD_CLAUSE,
        EntityType.METRIC,
        EntityType.RESPONSE_LEVEL,
        EntityType.TERM,
    }
)

_UID_PREFIX: dict[EntityType, str] = {
    EntityType.ENGINEERING_OBJECT: "eo",
    EntityType.RIVER: "rv",
    EntityType.ADMIN_REGION: "ar",
    EntityType.STANDARD_CLAUSE: "cl",
    EntityType.METRIC: "mt",
    EntityType.MANAGEMENT_UNIT: "mu",
    EntityType.RESPONSE_LEVEL: "rl",
    EntityType.EVENT: "ev",
    EntityType.TERM: "tm",
    EntityType.CONDITION: "cd",
    EntityType.MEASURE: "ms",
}


# ---------------- 动态本体（ontology profile）注册 ----------------
# 引擎与具体模型解耦：领域知识由 YAML profile 提供，加载时把类型元数据注册到这里，
# schema.py 本身不出现任何模型名。防洪链的枚举保持原样，两套类型体系共存。
_DYNAMIC_LABELS: dict[str, str] = {}          # 实体类型值 → Neo4j label
_DYNAMIC_LABELS_ZH: dict[str, str] = {}       # 实体类型值 → 中文名
_DYNAMIC_UID_META: dict[str, tuple[str, bool]] = {}  # 类型值 → (uid 前缀, 是否全局)
_DYNAMIC_REL_LABELS: dict[str, str] = {}      # 关系值 → Neo4j 关系类型
_DYNAMIC_REL_LABELS_ZH: dict[str, str] = {}   # 关系值 → 中文名
_DYNAMIC_ENDPOINTS: dict[str, dict[str, set[str]]] = {}  # 关系 → 主体类型 → {客体类型}


def type_value(value: Any) -> str:
    """统一取字符串值：枚举取 .value，普通字符串原样返回。"""
    return value.value if isinstance(value, Enum) else str(value)


def register_dynamic_types(
    entity_labels: dict[str, str] | None = None,
    entity_labels_zh: dict[str, str] | None = None,
    uid_meta: dict[str, tuple[str, bool]] | None = None,
    relation_labels: dict[str, str] | None = None,
    relation_labels_zh: dict[str, str] | None = None,
    endpoints: dict[str, dict[str, set[str]]] | None = None,
) -> None:
    """注册一个 ontology profile 的类型元数据（由 registry 加载 YAML 后调用）。"""
    _DYNAMIC_LABELS.update(entity_labels or {})
    _DYNAMIC_LABELS_ZH.update(entity_labels_zh or {})
    _DYNAMIC_UID_META.update(uid_meta or {})
    _DYNAMIC_REL_LABELS.update(relation_labels or {})
    _DYNAMIC_REL_LABELS_ZH.update(relation_labels_zh or {})
    _DYNAMIC_ENDPOINTS.update(endpoints or {})


def reset_dynamic_types() -> None:
    """测试用：清空动态注册，避免用例之间串数据。"""
    _DYNAMIC_LABELS.clear()
    _DYNAMIC_LABELS_ZH.clear()
    _DYNAMIC_UID_META.clear()
    _DYNAMIC_REL_LABELS.clear()
    _DYNAMIC_REL_LABELS_ZH.clear()
    _DYNAMIC_ENDPOINTS.clear()


def entity_label(value: Any) -> str:
    """实体类型 → Neo4j label（动态优先，其次防洪链枚举，最后原字符串）。"""
    tv = type_value(value)
    if tv in _DYNAMIC_LABELS:
        return _DYNAMIC_LABELS[tv]
    try:
        return ENTITY_LABELS[EntityType(tv)]
    except ValueError:
        return tv


def entity_label_zh(value: Any) -> str:
    tv = type_value(value)
    if tv in _DYNAMIC_LABELS_ZH:
        return _DYNAMIC_LABELS_ZH[tv]
    try:
        return ENTITY_LABELS_ZH[EntityType(tv)]
    except ValueError:
        return tv


def relation_label(value: Any) -> str:
    """关系 → Neo4j 关系类型（动态优先，其次枚举表，最后大写）。"""
    tv = type_value(value)
    if tv in _DYNAMIC_REL_LABELS:
        return _DYNAMIC_REL_LABELS[tv]
    try:
        return RELATION_LABELS[RelationType(tv)]
    except ValueError:
        return tv.upper()


def relation_label_zh(value: Any) -> str:
    tv = type_value(value)
    if tv in _DYNAMIC_REL_LABELS_ZH:
        return _DYNAMIC_REL_LABELS_ZH[tv]
    try:
        return RELATION_LABELS_ZH[RelationType(tv)]
    except ValueError:
        return tv


# 内置关系是历史小写（downstream_of），而 TBox 规范要求大写蛇形（DOWNSTREAM_OF）。
# 两者指同一个概念；落到存储层统一成大写，避免「同一条关系两种写法」并存——
# 否则按关系名过滤/分组时会各算各的，新建的关系和老数据也对不上。
_BUILTIN_REL_CANONICAL: dict[str, str] = {rt.value: rt.name for rt in RelationType}


def canonical_relation(value: Any) -> str:
    """关系名归一成大写蛇形（未知的自定义名也转大写，与 TBox 规范一致）。"""
    tv = type_value(value)
    return _BUILTIN_REL_CANONICAL.get(tv, tv.upper())

_QUOTES_RE = re.compile(r"[《》\"'“”‘’\s]+")
_SPACE_RE = re.compile(r"\s+")


def normalize_name(text: str | None) -> str:
    """名称规范化：全半角统一 + 去书名号引号 + 去多余空白 + 小写。

    不做「去掉市/县」这类激进归一（会把「重庆市」和「重庆县」混成一体），
    别名交给别名表处理。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).strip()
    s = _QUOTES_RE.sub("", s)
    s = _SPACE_RE.sub(" ", s)
    return s.lower()


def uid_for(entity_type: EntityType | str, name: str, kb_id: int = 0) -> str:
    """生成稳定 uid。

    - 全局实体（行政区/河流/条款/指标/响应等级/术语）：只看名称，跨库共享
    - 库内实体（工程/单位/事件/工况/措施）：把 kb_id 编进去，同名不同库不串
    - 动态本体类型：前缀与全局/私有由 profile 注册（`register_dynamic_types`）决定
    """
    tv = type_value(entity_type)
    meta = _DYNAMIC_UID_META.get(tv)
    if meta is not None:
        prefix, is_global = meta
    else:
        et = EntityType(tv)  # 未知类型直接报错，避免悄悄生成错误 uid
        prefix = _UID_PREFIX[et]
        is_global = et in GLOBAL_ENTITY_TYPES
    norm = normalize_name(name)
    key = norm if is_global else f"{kb_id}|{norm}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def edge_key(subject_uid: str, predicate: RelationType | str, object_uid: str) -> str:
    """边的稳定标识（不含版本号，证据表按它聚合）。"""
    pred = predicate.value if isinstance(predicate, RelationType) else str(predicate)
    return f"{subject_uid}|{pred}|{object_uid}"


def endpoints_allowed(
    predicate: RelationType | str,
    subject_type: EntityType | str,
    object_type: EntityType | str,
    registry: Any | None = None,
) -> bool:
    """端点合法性校验（抽取/导入都必须过这一关）。

    - 动态本体：优先查 profile 注册的端点表；传 registry 时以 registry 为准
    - 防洪链：不传 registry 且未注册时，走原枚举矩阵（行为完全不变）
    """
    pred = type_value(predicate)
    subj = type_value(subject_type)
    obj = type_value(object_type)

    if registry is not None:
        return bool(registry.endpoints_ok(pred, subj, obj))

    dynamic = _DYNAMIC_ENDPOINTS.get(pred)
    if dynamic is not None:
        return obj in dynamic.get(subj, set())

    try:
        rel = RelationType(pred)
        subj_enum = EntityType(subj)
        obj_enum = EntityType(obj)
    except ValueError:
        return False
    allowed = RELATION_ENDPOINTS.get(rel)
    if not allowed:
        return False
    return obj_enum in allowed.get(subj_enum, set())


@dataclass
class Node:
    """图节点。数值一律放 props（如 safe_discharge_m3s），便于精确筛选。"""

    uid: str
    entity_type: EntityType | str
    name: str
    props: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    kb_ids: list[int] = field(default_factory=list)
    doc_ids: list[int] = field(default_factory=list)
    version_ids: list[int] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        """类型字符串（枚举或动态字符串统一出口）。"""
        return type_value(self.entity_type)

    @property
    def label(self) -> str:
        return entity_label(self.entity_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "entity_type": self.type_name,
            "name": self.name,
            "props": dict(self.props),
            "aliases": list(self.aliases),
            "kb_ids": list(self.kb_ids),
            "doc_ids": list(self.doc_ids),
            "version_ids": list(self.version_ids),
        }


@dataclass
class Edge:
    """图关系。每条边必须能回溯到原文（chunk_ids 或人工来源说明）。"""

    subject_uid: str
    predicate: RelationType | str
    object_uid: str
    kb_id: int
    doc_id: int | None = None
    version_id: int | None = None
    chunk_ids: list[int] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    props: dict[str, Any] = field(default_factory=dict)
    extractor: str = "rule"  # rule / llm / manual
    confidence: float = 1.0
    status: str = "auto"  # auto / confirmed / rejected
    evidence_text: str = ""

    @property
    def key(self) -> str:
        return edge_key(self.subject_uid, self.predicate, self.object_uid)

    @property
    def predicate_name(self) -> str:
        """关系字符串（枚举或动态字符串统一出口）。"""
        return type_value(self.predicate)

    @property
    def rel_label(self) -> str:
        return relation_label(self.predicate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "subject": self.subject_uid,
            "predicate": self.predicate_name,
            "predicate_label": self.rel_label,
            "object": self.object_uid,
            "kb_id": self.kb_id,
            "doc_id": self.doc_id,
            "version_id": self.version_id,
            "chunk_ids": list(self.chunk_ids),
            "evidence_ids": list(self.evidence_ids),
            "props": dict(self.props),
            "extractor": self.extractor,
            "confidence": self.confidence,
            "status": self.status,
            "evidence_text": self.evidence_text,
        }


@dataclass
class GraphSlice:
    """一次图查询的结果（画图或拼 prompt 用）。"""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    truncated: bool = False

    def node_map(self) -> dict[str, Node]:
        return {n.uid: n for n in self.nodes}


@dataclass
class ExtractionResult:
    """抽取/导入的产物 + 被拒明细（逐条拒绝，不因一条坏数据丢整块）。"""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
