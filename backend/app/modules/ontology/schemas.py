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
    # 默认按**并集**合并（老行为：重导 Excel 不会冲掉别的来源的证据）。
    # 界面上的证据是列表，用户删掉一条必须真的消失，那时传 true 用整份列表替换。
    evidence_replace: bool = Field(
        False, description="true = 用本次提交的证据整份替换（界面上删除依据时用）"
    )
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
    # 切进这个库能看见几个节点 = max(PG 实例数, 图里该库声明类型的节点数)
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
    # —— 自动抽取规则（F4）——
    extract_pattern: str = Field(
        "", max_length=300,
        description="正则；可写 {name} 表示实例名。留空表示这个属性不参与自动抽取",
    )
    extract_unit: str = Field("", max_length=20)
    extract_map: dict[str, str] = Field(
        default_factory=dict, description="原文写法 → 枚举值，如 {敏感: 高}"
    )


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
    # 实例当前这个字段的值：审核时和提案值并排看，冲突一眼可见（P2）
    current_value: Any = None
    method: str
    confidence: float
    evidence: dict[str, Any] | None = None
    status: str
    created_at: datetime | None = None


class ProposalAcceptIn(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=200)


class RunRecordIn(BaseModel):
    """一次运行记录（O8-2）：建模 agent 跑完率定后上报。

    `run_id` 是幂等键：同一个 id 重复上报只更新值，不会堆出第二个 Run 节点。
    """

    profile_id: str | None = None
    run_id: str = Field(..., min_length=1, max_length=120, description="幂等键，上报方生成")
    started_at: str | None = Field(None, max_length=40, description="如 2026-11-18T10:00:00")
    objective: str | None = Field(None, max_length=500, description="目标函数 / 本次目标")
    strategy: str | None = Field(None, max_length=100, description="率定策略，如 SCE-UA")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="{参数名: 取值}；参数必须已在库中"
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict, description="{指标名: 取值}；库里没有的指标会自动建空节点"
    )
    note: str | None = Field(None, max_length=2000)
    kb_id: int = Field(0, ge=0)


class AnchorRunIn(BaseModel):
    """P2 证据自动锚定：给已有实例找原文出处（不改值）。"""

    profile_id: str | None = None
    kb_id: int | None = Field(None, description="只在某个知识库里找；不传 = 全库")
    instance_uids: list[str] | None = Field(None, description="只锚定这些实例；不传 = 全库实例")
    max_instances: int = Field(20, ge=1, le=200, description="一次最多锚定多少个实例（花钱的闸）")
    top_k: int = Field(6, ge=1, le=20, description="每个实例最多看几段候选切片")
    dry_run: bool = Field(False, description="true = 只预演，不写待审区")
