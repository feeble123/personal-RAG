"""本体管理接口的输入/输出结构。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EntityTypeStr = str


class InstanceOut(BaseModel):
    """一条本体实例（真相源在 PG）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: str
    profile_id: str
    entity_type: str
    name: str
    kb_id: int
    props: dict[str, Any] = Field(default_factory=dict)
    ranges: list[Any] | None = None
    evidence: list[Any] | None = None
    source: str
    status: str
    updated_at: datetime | None = None


class InstanceCreate(BaseModel):
    entity_type: EntityTypeStr = Field(..., description="实体类型，如 Parameter")
    name: str = Field(..., min_length=1, max_length=200)
    profile_id: str | None = Field(None, description="为空则用 ONTOLOGY_PROFILE")
    kb_id: int = Field(0, ge=0, description="0 = 全局共享")
    props: dict[str, Any] = Field(default_factory=dict)
    ranges: list[Any] = Field(default_factory=list)
    evidence: list[Any] = Field(default_factory=list)
    source: str = Field("manual", description="manual / import / extracted")
    status: Literal["active", "archived"] = "active"


class InstancePatch(BaseModel):
    """只改传了的字段；都要改就都传。"""

    props: dict[str, Any] | None = None
    ranges: list[Any] | None = None
    evidence: list[Any] | None = None
    status: Literal["active", "archived"] | None = None
    source: str | None = None


class CardOut(BaseModel):
    parameter: str
    name: str
    group: str
    unit: str
    range_level: str
    confidence: float
    evidence_level: str = "none"
    range: list[float | None] | None = None
    physical_range: list[float | None] | None = None
    initial: float | None = None
    sensitivity: str | None = None
    narrowed_by: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    dry_run: bool
    profile_id: str
    rows: int
    drafts: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    preview: list[dict[str, Any]] = Field(default_factory=list)


class ReloadOut(BaseModel):
    profile_id: str
    entity_types: int
    relation_types: int


class AskIn(BaseModel):
    """对外本体问答（agent 用）：问题 → 事实包。"""

    question: str = Field(..., min_length=1, max_length=500)
    kb_id: int | None = Field(None, description="限定知识库；缺省跨全库")
    profile_id: str | None = Field(None, description="本体 profile，缺省用配置默认值")
    climate: str | None = Field(None, description="湿润/半湿润/半干旱/干旱")
    flood_type: str | None = Field(None, description="单峰/多峰")
    min_facts: int | None = Field(None, ge=0, le=50, description="事实条数门槛，低于则回退向量检索")


# ===== 关系（ABox 的边）=====


class RelationOut(BaseModel):
    """一条本体关系（真相源在 PG `ontology_relations`）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: str
    subject_uid: str
    predicate: str
    object_uid: str
    kb_id: int
    props: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Any] | None = None
    source: str
    status: str
    updated_at: datetime | None = None


class RelationCreate(BaseModel):
    subject_uid: str = Field(..., min_length=1, max_length=80)
    predicate: str = Field(..., min_length=1, max_length=60, description="关系类型，须在 TBox 中定义")
    object_uid: str = Field(..., min_length=1, max_length=80)
    profile_id: str | None = None
    kb_id: int = Field(0, ge=0, description="0 = 全局共享")
    props: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Any] = Field(default_factory=list)
    source: str = Field("manual", description="manual / import / extracted")
    status: Literal["active", "archived"] = "active"


class RelationPatch(BaseModel):
    """只改传了的字段。主语/宾语不许改——改端点等于换一条关系，删了重建更清楚。"""

    predicate: str | None = Field(None, min_length=1, max_length=60)
    props: dict[str, Any] | None = None
    evidence: list[Any] | None = None
    status: Literal["active", "archived"] | None = None
    source: str | None = None


# ---------------- 图谱库 ----------------

class LibraryOut(BaseModel):
    """一个图谱库 = 一份 TBox（YAML）+ 属于它的实例与关系。"""

    profile_id: str
    display_name: str
    description: str = ""
    file: str
    entity_type_count: int = 0
    relation_type_count: int = 0
    instance_count: int = 0
    relation_count: int = 0
    # 图里真实节点数（按本库声明的实体类型收口）；界面上显示的就是它
    node_count: int = 0
    is_default: bool = False
    error: str | None = Field(None, description="TBox 解析失败时的原因")


class LibraryCreate(BaseModel):
    profile_id: str = Field(..., min_length=2, max_length=40, description="库标识，同时是文件名与 ?profile= 取值")
    display_name: str = Field(..., min_length=1, max_length=60, description="界面上显示的库名")
    description: str = Field("", max_length=300)
    template: str = Field(
        "tuning",
        max_length=40,
        description="从哪个库复制类型定义；blank = 空白库（只有最基本的参数类型）",
    )


# ---------------- TBox 在线编辑（F3）----------------

class EntityTypeIn(BaseModel):
    """新建/修改一个实体类型。name 与 uid_prefix 决定实例 uid，改建不建改。"""

    name: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    display_name: str = Field(..., min_length=1, max_length=40)
    uid_prefix: str = Field(..., pattern=r"^[a-z]{2,4}$", description="2~4 位小写字母，实例 uid 前缀")
    scope: Literal["local", "global"] = Field("local", description="local=按库隔离，global=跨库共享")


class AttributeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: Literal["string", "text", "number", "range", "enum", "list", "boolean"] = "string"
    values: list[str] = Field(default_factory=list, description="enum 类型的可选值")
    required: bool = False
    description: str = Field("", max_length=200)


class RelationTypeIn(BaseModel):
    """新建/修改关系类型。name 就是边上的 predicate，建成后不可改。"""

    name: str = Field(..., min_length=1, max_length=60, pattern=r"^[A-Z][A-Z0-9_]*$",
                      description="大写蛇形，如 DOWNSTREAM_OF")
    display_name: str = Field(..., min_length=1, max_length=40)
    source: str = Field(..., min_length=1, max_length=40, description="主语实体类型")
    target: str = Field(..., min_length=1, max_length=40, description="宾语实体类型")
    target_types: list[str] = Field(default_factory=list, description="允许的宾语类型（可多选，空=只用 target）")
    description: str = Field("", max_length=200)


# ---------------- 自动抽取（F4）----------------

class ExtractRunIn(BaseModel):
    profile_id: str | None = None
    kb_id: int | None = Field(None, description="限定知识库；缺省跨全库")
    use_llm: bool = Field(False, description="规则抽不到的再交给大模型（慢、费 token）")
    dry_run: bool = Field(False, description="只返回结果，不写待审区")


class ProposalOut(BaseModel):
    id: int
    profile_id: str
    instance_uid: str
    instance_name: str = ""
    entity_type: str
    attribute: str
    value: Any
    method: str
    confidence: float
    evidence: dict[str, Any] | None = None
    status: str
    created_at: datetime | None = None


class ProposalAcceptIn(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=200)
