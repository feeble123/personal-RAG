"""通用本体引擎：TBox（YAML profile）的加载、校验、缓存与注册。

设计原则
--------
1. **引擎不认识任何具体模型**：领域知识只是 `profiles/` 下的 YAML 数据；
   代码里出现模型名就是设计错误。
2. 加载成功后把类型元数据注册到 `graph.schema`，`Node`/`Edge`/`GraphStore`
   即可处理动态类型，无需为每种本体改代码。
3. `profile` 支持 `extends`：具体领域的 profile 可以叠加在通用本体之上，
   只写增量（后续模型 profile 就靠这个复用通用调参本体）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.services.graph import schema as graph_schema

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
# 默认调参本体；流程骨架（process_skeleton）按需再加载，类型注册会自动合并
DEFAULT_PROFILE = "tuning"

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_PREFIX_RE = re.compile(r"^[a-z]{2,4}$")
_ATTR_TYPES = {"string", "text", "number", "range", "enum", "list", "boolean"}
_SCOPES = {"local", "global"}
#: 关系可以声明的链路档位。必须与 `chain.PHASE_ORDER` 里的档位一致
#: （`subject`/`other` 是编排层自己产生的，不由 YAML 声明）；有单测守着这条一致性。
PHASE_VALUES = ("basis", "flow", "actor", "means", "impact", "response")

#: 约束类型。**每一种都必须能追溯到一份 CQ 清单里的条目**
#: （见 `docs/本体能力问题清单.md` §8）；没有能力问题支撑的约束一律不加，
#: 否则约束会变成"凭感觉定的规矩"，最后没人敢改。
#:
#: - `required_attribute`：某类型实例必须有这个属性
#: - `min_count` / `max_count`：某类型实例沿关系（可多选）的边数下限 / 上限
#: - `unique_attribute`：同一类型内该属性的取值不能重复
#: - `coverage`：**逐条核对**。实例说"原文共有 N 条"，每条边自己说"我对应第几条"，
#:   要求 1..N 每一条都有人认领。比 min_count 严：只数条数的话，把一条拆成两条
#:   就能凑够数量、把另一条整个漏掉——实测Ⅳ级就漏了原文第 (2) 条却"通过"了。
CONSTRAINT_TYPES = (
    "required_attribute",
    "min_count",
    "max_count",
    "unique_attribute",
    "coverage",
)
#: 严重级：error 拦住建库，warning 只提示
SEVERITIES = ("error", "warning")
#: 计数类约束看的方向：out = 实例出发的边，in = 指向实例的边
CONSTRAINT_DIRECTIONS = ("out", "in")

#: 规则类型。
#: `classify`：拿问题里工况的一个数值去比区间，命中就落到某类型里符合 `target_match` 的实例上。
#: 这是**本体相对 GraphRAG 的实质差别**——GraphRAG 只能告诉你"文档里出现过的关系"，
#: 规则能告诉你"按定义必然成立的关系"，因此 300 年一遇这种没人手工连过边的工况
#: 也能自动归到正确的等级。
RULE_TYPES = ("classify",)
#: `when` 里能用的比较算子
RULE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")


class ProfileError(ValueError):
    """profile 文件不合法（消息里带文件与字段位置，便于排查）。"""


@dataclass(frozen=True)
class AttributeDef:
    name: str
    type: str
    required: bool = False
    values: tuple[str, ...] = ()
    description: str = ""
    # 自动抽取规则（F4）：{pattern/unit} —— 有它才会去知识库里找这个属性的值
    extract: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "type": self.type, "required": self.required}
        if self.values:
            out["values"] = list(self.values)
        if self.description:
            out["description"] = self.description
        if self.extract:
            out["extract"] = dict(self.extract)
        return out


@dataclass(frozen=True)
class EntityTypeDef:
    name: str
    display_name: str
    uid_prefix: str
    scope: str  # local / global
    label: str  # Neo4j 标签
    attributes: dict[str, AttributeDef] = field(default_factory=dict)

    @property
    def is_global(self) -> bool:
        return self.scope == "global"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "uid_prefix": self.uid_prefix,
            "scope": self.scope,
            "label": self.label,
            "attributes": [a.to_dict() for a in self.attributes.values()],
        }


@dataclass(frozen=True)
class RelationTypeDef:
    name: str
    source: str
    target: str
    description: str = ""
    # 界面展示用的中文名（如 PART_OF → 属于）；缺省回落到 description
    display_name: str = ""
    target_types: tuple[str, ...] = ()
    inverse_of: str | None = None
    #: 链路编排里的档位（basis/flow/actor/means/impact/response）。
    #: 引擎内置了一张「历史关系名 → 档位」的表，但只认那 22 个老名字；
    #: 新领域的 profile 在 YAML 里直接声明，编排层就不用为每个领域改代码。
    phase: str = ""
    #: 这条关系是不是**包含关系**（"A 属于 B"）。标了它的边会被检索层当作继承用：
    #: 问 A 的时候，B 身上的事实一并算作 A 的（标 `inherited`）。
    #: 没有这个标记，`INSTANCE_OF` 就只是一条普通边，不传递也不继承。
    subsumption: bool = False

    def allows_object(self, object_type: str) -> bool:
        return object_type in (self.target_types or (self.target,))

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "display_name": self.display_name or self.description or self.name,
            "description": self.description,
        }
        if self.target_types:
            out["target_types"] = list(self.target_types)
        if self.inverse_of:
            out["inverse_of"] = self.inverse_of
        if self.phase:
            out["phase"] = self.phase
        if self.subsumption:
            out["subsumption"] = True
        return out


@dataclass(frozen=True)
class ConstraintDef:
    """一条本体约束（TBox 里声明，建库时对 ABox 求值）。

    为什么约束要写在 TBox 而不是脚本里：**它是"这套本体应该长什么样"的定义**，
    属于模式的一部分。写在 Python 里的话，换个本体就查不了，也没法追溯到需求。
    `cq` 字段把每条约束钉回能力问题清单——约束报错时能直接看出"缺的是哪一项能力"。
    """

    id: str
    type: str
    entity: str
    severity: str = "error"
    #: 计数类约束看哪些关系；留空表示"任何关系"
    relation: tuple[str, ...] = ()
    direction: str = "out"
    #: 计数时只数指向这个类型的边（如"等级 → 措施"只数 measure）
    target_type: str = ""
    #: required_attribute / unique_attribute 的目标属性
    attribute: str = ""
    min: int | None = None
    max: int | None = None
    #: 下限也可以从实例属性读（每级响应要求的措施条数不一样，数字属于 ABox）
    min_attr: str = ""
    #: coverage 用：实例属性里写的"原文共几条"
    total_attr: str = ""
    #: coverage 用：边属性里写的"我对应原文第几条"
    item_attr: str = ""
    #: 追溯到哪条能力问题（docs/本体能力问题清单.md 的编号）
    cq: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "entity": self.entity,
            "severity": self.severity,
        }
        if self.relation:
            out["relation"] = list(self.relation)
        if self.direction != "out":
            out["direction"] = self.direction
        if self.target_type:
            out["target_type"] = self.target_type
        if self.attribute:
            out["attribute"] = self.attribute
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.min_attr:
            out["min_attr"] = self.min_attr
        if self.total_attr:
            out["total_attr"] = self.total_attr
        if self.item_attr:
            out["item_attr"] = self.item_attr
        if self.cq:
            out["cq"] = self.cq
        if self.message:
            out["message"] = self.message
        return out


@dataclass(frozen=True)
class RuleDef:
    """一条推导规则（TBox 里声明，检索时对"问题里的工况"求值）。

    为什么规则要写在 TBox：**"多少年一遇归哪一级"是这个领域的定义，不是程序逻辑**。
    以前它硬编码在 `conditions.py`，导致每新增一个工况都得回代码里改，或者手工连边。
    写进 YAML 之后，加一档只要加一条规则，引擎一行不用动。
    """

    id: str
    type: str
    #: 拿问题工况的哪个数值来比（`return_period` / `value` 之类，取自 `Condition`）
    key: str
    #: 算子 → 阈值，多个条件是 AND
    when: dict[str, float]
    #: 命中后落到哪条关系上
    relation: str
    #: 落到哪个类型的实例上
    target_type: str
    #: 用哪些属性把这一个实例挑出来（如 `{level: Ⅱ}`）
    target_match: dict[str, Any]
    cq: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "type": self.type,
            "key": self.key,
            "when": dict(self.when),
            "emit": {
                "relation": self.relation,
                "target_type": self.target_type,
                "target_match": dict(self.target_match),
            },
        }
        if self.cq:
            out["cq"] = self.cq
        if self.message:
            out["message"] = self.message
        return out


class OntologyRegistry:
    """一个 profile 的运行时视图（只读）。"""

    def __init__(
        self,
        profile_id: str,
        version: int,
        domain: str,
        entity_types: dict[str, EntityTypeDef],
        relation_types: dict[str, RelationTypeDef],
        source_path: Path | None = None,
        display_name: str = "",
        description: str = "",
        constraints: dict[str, ConstraintDef] | None = None,
        rules: dict[str, RuleDef] | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.version = version
        self.domain = domain
        self.entity_types = entity_types
        self.relation_types = relation_types
        self.constraints = constraints or {}
        self.rules = rules or {}
        self.source_path = source_path
        # 图谱库的展示信息：界面上「库名」就是它，缺省回落到 profile_id。
        # 注意**不能**叫 display_name——那个名字已经是「类型 → 中文名」的方法，
        # 用同名实例属性会把方法覆盖掉（调用时 TypeError: 'str' object is not callable）。
        self.library_name = display_name or profile_id
        self.description = description

    # ---------- 查询 ----------
    def entity(self, name: str) -> EntityTypeDef | None:
        return self.entity_types.get(name)

    def relation(self, name: str) -> RelationTypeDef | None:
        return self.relation_types.get(name)

    def constraint(self, name: str) -> ConstraintDef | None:
        return self.constraints.get(name)

    def rule(self, name: str) -> RuleDef | None:
        return self.rules.get(name)

    def subsumption_relations(self) -> tuple[str, ...]:
        """哪些关系是包含关系（标了 `subsumption: true`）。

        检索层用它做继承：问 A 的时候要能拿到 A 所属的 B 身上的事实。
        引擎不认识 `INSTANCE_OF` 这个名字——换任何本体，只要标了这个开关就生效。
        """
        return tuple(r.name for r in self.relation_types.values() if r.subsumption)

    def has_entity(self, name: str) -> bool:
        return name in self.entity_types

    def endpoints_ok(self, relation: str, subject_type: str, object_type: str) -> bool:
        rel = self.relation_types.get(relation)
        if rel is None:
            return False
        if rel.source != subject_type:
            return False
        return rel.allows_object(object_type)

    def uid_for(self, entity_type: str, name: str, kb_id: int = 0) -> str:
        """按 profile 声明的 uid 前缀与作用域生成稳定 uid。"""
        self._require_entity(entity_type)
        return graph_schema.uid_for(entity_type, name, kb_id)

    def label_of(self, entity_type: str) -> str:
        ent = self.entity_types.get(entity_type)
        return ent.label if ent else entity_type

    def display_name(self, entity_type: str) -> str:
        ent = self.entity_types.get(entity_type)
        return ent.display_name if ent else entity_type

    def indexed_props(self, entity_type: str) -> list[str]:
        ent = self.entity_types.get(entity_type)
        if ent is None:
            return []
        return [a.name for a in ent.attributes.values() if a.type in ("number", "enum")]

    def _require_entity(self, entity_type: str) -> EntityTypeDef:
        ent = self.entity_types.get(entity_type)
        if ent is None:
            raise ProfileError(
                f"profile {self.profile_id!r} 未定义实体类型 {entity_type!r}；"
                f"可用类型：{', '.join(sorted(self.entity_types))}"
            )
        return ent

    # ---------- 对外快照 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.library_name,
            "description": self.description,
            "version": self.version,
            "domain": self.domain,
            "entity_types": [e.to_dict() for e in self.entity_types.values()],
            "relation_types": [r.to_dict() for r in self.relation_types.values()],
            "constraints": [c.to_dict() for c in self.constraints.values()],
            "rules": [r.to_dict() for r in self.rules.values()],
        }

    # ---------- 注册到 schema ----------
    def register(self) -> None:
        """把本 profile 的类型元数据注册到 graph.schema（Node/Edge/Store 立即生效）。"""
        entity_labels = {e.name: e.label for e in self.entity_types.values()}
        entity_labels_zh = {e.name: e.display_name for e in self.entity_types.values()}
        uid_meta = {e.name: (e.uid_prefix, e.is_global) for e in self.entity_types.values()}
        relation_labels = {r.name: r.name for r in self.relation_types.values()}
        relation_zh = {
            r.name: r.display_name or r.description or r.name
            for r in self.relation_types.values()
        }
        endpoints: dict[str, dict[str, set[str]]] = {}
        for rel in self.relation_types.values():
            allowed = endpoints.setdefault(rel.name, {})
            allowed.setdefault(rel.source, set()).update(rel.target_types or (rel.target,))
        graph_schema.register_dynamic_types(
            entity_labels=entity_labels,
            entity_labels_zh=entity_labels_zh,
            uid_meta=uid_meta,
            relation_labels=relation_labels,
            relation_labels_zh=relation_zh,
            endpoints=endpoints,
        )


# ---------------- 加载 ----------------

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProfileError(f"profile 文件不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"YAML 解析失败（{path.name}）：{exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"profile 顶层必须是映射（{path.name}）")
    return data


def _resolve(profile: str | Path) -> Path:
    if isinstance(profile, Path):
        return profile if profile.is_absolute() else (PROFILES_DIR / profile)
    candidate = Path(profile)
    if candidate.suffix in (".yaml", ".yml") or candidate.exists():
        return candidate if candidate.is_absolute() else (PROFILES_DIR / candidate)
    return PROFILES_DIR / f"{profile}.yaml"


def _parse_entities(raw: Any, path: Path) -> dict[str, EntityTypeDef]:
    if not isinstance(raw, list) or not raw:
        raise ProfileError(f"{path.name}: entity_types 必须是非空列表")
    out: dict[str, EntityTypeDef] = {}
    for idx, item in enumerate(raw):
        where = f"{path.name}: entity_types[{idx}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where} 必须是映射")
        name = str(item.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ProfileError(f"{where}.name 非法：{name!r}（字母开头，仅字母数字下划线）")
        if name in out:
            raise ProfileError(f"{where}.name 重复：{name}")
        prefix = str(item.get("uid_prefix") or "").strip()
        if not _PREFIX_RE.match(prefix):
            raise ProfileError(f"{where}.uid_prefix 必须是 2~4 位小写字母，收到 {prefix!r}")
        scope = str(item.get("scope") or "global").strip()
        if scope not in _SCOPES:
            raise ProfileError(f"{where}.scope 仅支持 local/global，收到 {scope!r}")

        attributes: dict[str, AttributeDef] = {}
        for a_idx, attr in enumerate(item.get("attributes") or []):
            a_where = f"{where}.attributes[{a_idx}]"
            if not isinstance(attr, dict) or not attr.get("name"):
                raise ProfileError(f"{a_where} 缺少 name")
            a_name = str(attr["name"]).strip()
            a_type = str(attr.get("type") or "string").strip()
            if a_type not in _ATTR_TYPES:
                raise ProfileError(
                    f"{a_where}.type 非法：{a_type!r}（可选：{', '.join(sorted(_ATTR_TYPES))}）"
                )
            values = tuple(str(v) for v in (attr.get("values") or ()))
            if a_type == "enum" and not values:
                raise ProfileError(f"{a_where} 是 enum 但没给 values")
            if a_name in attributes:
                raise ProfileError(f"{a_where}.name 重复：{a_name}")
            extract = attr.get("extract")
            if extract is not None and not isinstance(extract, dict):
                raise ProfileError(f"{a_where}.extract 必须是映射（pattern / unit）")
            if isinstance(extract, dict) and not extract.get("pattern"):
                raise ProfileError(f"{a_where}.extract 缺少 pattern（正则）")
            attributes[a_name] = AttributeDef(
                name=a_name,
                type=a_type,
                required=bool(attr.get("required", False)),
                values=values,
                description=str(attr.get("description") or ""),
                extract=dict(extract) if isinstance(extract, dict) else None,
            )

        out[name] = EntityTypeDef(
            name=name,
            display_name=str(item.get("display_name") or name),
            uid_prefix=prefix,
            scope=scope,
            label=str(item.get("label") or name),
            attributes=attributes,
        )
    return out


def _parse_relations(raw: Any, entities: dict[str, EntityTypeDef], path: Path) -> dict[str, RelationTypeDef]:
    if not isinstance(raw, list):
        raise ProfileError(f"{path.name}: relation_types 必须是列表")
    out: dict[str, RelationTypeDef] = {}
    for idx, item in enumerate(raw):
        where = f"{path.name}: relation_types[{idx}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where} 必须是映射")
        name = str(item.get("name") or "").strip()
        if not name or not re.match(r"^[A-Z][A-Z0-9_]*$", name):
            raise ProfileError(f"{where}.name 必须是大写蛇形（如 AFFECTS），收到 {name!r}")
        if name in out:
            raise ProfileError(f"{where}.name 重复：{name}")
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        target_types = tuple(str(t) for t in (item.get("target_types") or ()))
        for ref in (source, target, *target_types):
            if ref and ref not in entities:
                raise ProfileError(f"{where} 引用了未定义的实体类型：{ref!r}")
        if not source or not target:
            raise ProfileError(f"{where} 必须声明 source 与 target")
        phase = str(item.get("phase") or "").strip()
        if phase and phase not in PHASE_VALUES:
            raise ProfileError(
                f"{where}.phase 只能是 {'/'.join(PHASE_VALUES)}，收到 {phase!r}"
            )
        out[name] = RelationTypeDef(
            name=name,
            source=source,
            target=target,
            description=str(item.get("description") or ""),
            display_name=str(item.get("display_name") or ""),
            target_types=target_types,
            inverse_of=item.get("inverse_of"),
            phase=phase,
            subsumption=bool(item.get("subsumption", False)),
        )
    return out


def _constraint_id(item: dict[str, Any]) -> str:
    """约束 id：优先用声明的，没写就按内容生成一个（兼容老 profile）。

    生成规则要**稳定**：同一个约束改不改顺序都该得到同一个 id，
    否则重跑时 id 变了，历史报告就没法比对。
    """
    cid = str(item.get("id") or "").strip()
    if cid:
        return cid
    ctype = str(item.get("type") or "").strip()
    entity = str(item.get("entity") or "").strip()
    attr = str(item.get("attribute") or "").strip()
    rel = item.get("relation")
    rel_first = ""
    if isinstance(rel, str):
        rel_first = rel.strip()
    elif isinstance(rel, (list, tuple)) and rel:
        rel_first = str(rel[0]).strip()
    tail = attr or rel_first
    return f"{ctype}:{entity}" + (f".{tail}" if tail else "")


def _relations_of(item: dict[str, Any], where: str) -> tuple[str, ...]:
    raw = item.get("relation")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for value in raw:
            text = str(value).strip()
            if not text:
                raise ProfileError(f"{where}.relation 里有空值")
            out.append(text)
        return tuple(out)
    raise ProfileError(f"{where}.relation 必须是字符串或字符串列表")


def _int_field(item: dict[str, Any], key: str, where: str) -> int | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where}.{key} 必须是整数，收到 {value!r}")
    number = int(value)
    if number != value:
        raise ProfileError(f"{where}.{key} 必须是整数，收到 {value!r}")
    if number < 0:
        raise ProfileError(f"{where}.{key} 不能为负，收到 {number}")
    return number


def _parse_constraints(
    raw: Any,
    entities: dict[str, EntityTypeDef],
    relations: dict[str, RelationTypeDef],
    path: Path,
) -> dict[str, ConstraintDef]:
    """解析 `constraints:` 段。

    这里做的是**约束定义本身**的合法性校验（写错了要在加载期就炸），
    真正的 ABox 求值在 `constraints.check_constraints`。
    两者分开：定义写错是代码问题，数据不满足是数据问题，报错要能分清。
    """
    out: dict[str, ConstraintDef] = {}
    for idx, item in enumerate(raw or []):
        where = f"{path.name}: constraints[{idx}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where} 必须是映射")

        ctype = str(item.get("type") or "").strip()
        if ctype not in CONSTRAINT_TYPES:
            raise ProfileError(
                f"{where}.type 非法：{ctype!r}（可选：{', '.join(CONSTRAINT_TYPES)}）"
            )
        entity = str(item.get("entity") or "").strip()
        ent = entities.get(entity)
        if ent is None:
            raise ProfileError(f"{where} 引用了未定义实体 {entity!r}")

        severity = str(item.get("severity") or "error").strip()
        if severity not in SEVERITIES:
            raise ProfileError(
                f"{where}.severity 只能是 {'/'.join(SEVERITIES)}，收到 {severity!r}"
            )

        attribute = str(item.get("attribute") or "").strip()
        rels = _relations_of(item, where)
        direction = str(item.get("direction") or "out").strip()
        if direction not in CONSTRAINT_DIRECTIONS:
            raise ProfileError(
                f"{where}.direction 只能是 {'/'.join(CONSTRAINT_DIRECTIONS)}，收到 {direction!r}"
            )
        target_type = str(item.get("target_type") or "").strip()
        min_value = _int_field(item, "min", where)
        max_value = _int_field(item, "max", where)
        min_attr = str(item.get("min_attr") or "").strip()
        total_attr = str(item.get("total_attr") or "").strip()
        item_attr = str(item.get("item_attr") or "").strip()

        # ---- 按类型分别校验字段组合 ----
        if ctype in ("required_attribute", "unique_attribute"):
            if not attribute:
                raise ProfileError(f"{where} 是 {ctype}，必须声明 attribute")
            if attribute not in ent.attributes:
                raise ProfileError(f"{where} 引用了未定义属性 {entity}.{attribute}")
            if rels:
                raise ProfileError(f"{where} 是 {ctype}，不该声明 relation")

        if ctype in ("min_count", "max_count", "coverage"):
            if not rels:
                raise ProfileError(f"{where} 是 {ctype}，必须声明至少一个 relation")
            for rel_name in rels:
                rel = relations.get(rel_name)
                if rel is None:
                    raise ProfileError(f"{where}.relation 引用了未定义关系 {rel_name!r}")
                if direction == "out" and rel.source != entity:
                    raise ProfileError(
                        f"{where}.relation {rel_name} 的主语是 {rel.source}，"
                        f"与 entity={entity} 不符（方向 out 要求实例是主语）"
                    )
                if direction == "in" and not rel.allows_object(entity):
                    raise ProfileError(
                        f"{where}.relation {rel_name} 的宾语不是 {entity}，"
                        f"与 direction=in 不符（要求实例是宾语）"
                    )
            if target_type:
                if target_type not in entities:
                    raise ProfileError(f"{where}.target_type 引用了未定义实体 {target_type!r}")
                # 多个关系取并集时（如「STIPULATES 或 TRIGGERS」），只要**有一个**关系
                # 能连到 target_type 就说明写法成立——其余关系天然贡献 0 条，
                # 那不是错误，是过滤条件本来就把它们筛掉了。
                reachable = any(
                    (
                        relations[name].source == target_type
                        if direction == "in"
                        else relations[name].allows_object(target_type)
                    )
                    for name in rels
                )
                if not reachable:
                    raise ProfileError(
                        f"{where}.target_type {target_type} 不是 "
                        f"{'/'.join(rels)} 在 direction={direction} 下的另一端"
                    )
            if attribute:
                raise ProfileError(f"{where} 是 {ctype}，不用声明 attribute")
            if min_attr and min_attr not in ent.attributes:
                raise ProfileError(f"{where}.min_attr 引用了未定义属性 {entity}.{min_attr}")

        if ctype == "min_count" and min_value is None and not min_attr:
            raise ProfileError(f"{where} 是 min_count，必须声明 min 或 min_attr")
        if ctype == "max_count" and max_value is None:
            raise ProfileError(f"{where} 是 max_count，必须声明 max")

        if ctype == "coverage":
            if not total_attr:
                raise ProfileError(f"{where} 是 coverage，必须声明 total_attr（实例上的条目总数）")
            if total_attr not in ent.attributes:
                raise ProfileError(f"{where}.total_attr 引用了未定义属性 {entity}.{total_attr}")
            if not item_attr:
                raise ProfileError(f"{where} 是 coverage，必须声明 item_attr（边上的条目序号）")
            if min_value is not None or max_value is not None:
                raise ProfileError(f"{where} 是 coverage，下限由 total_attr 决定，不用写 min/max")

        cid = _constraint_id(item)
        if cid in out:
            raise ProfileError(f"{where}.id 与前面的约束重复：{cid}")

        out[cid] = ConstraintDef(
            id=cid,
            type=ctype,
            entity=entity,
            severity=severity,
            relation=rels,
            direction=direction,
            target_type=target_type,
            attribute=attribute,
            min=min_value,
            max=max_value,
            min_attr=min_attr,
            total_attr=total_attr,
            item_attr=item_attr,
            cq=str(item.get("cq") or "").strip(),
            message=str(item.get("message") or "").strip(),
        )
    return out


def _parse_rules(
    raw: Any,
    entities: dict[str, EntityTypeDef],
    relations: dict[str, RelationTypeDef],
    path: Path,
) -> dict[str, RuleDef]:
    """解析 `rules:` 段。

    规则定义写错（算子拼错、目标类型不存在、挑实例的属性没声明）在**加载期**就炸；
    规则命中与否是**数据**，不在这里判断。
    """
    out: dict[str, RuleDef] = {}
    for idx, item in enumerate(raw or []):
        where = f"{path.name}: rules[{idx}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where} 必须是映射")

        rtype = str(item.get("type") or "classify").strip()
        if rtype not in RULE_TYPES:
            raise ProfileError(f"{where}.type 非法：{rtype!r}（可选：{', '.join(RULE_TYPES)}）")
        rid = str(item.get("id") or "").strip()
        if not rid:
            raise ProfileError(f"{where} 缺少 id")
        if rid in out:
            raise ProfileError(f"{where}.id 重复：{rid}")

        key = str(item.get("key") or "").strip()
        if not key:
            raise ProfileError(f"{where} 缺少 key（拿问题工况的哪个数值来比）")

        when_raw = item.get("when")
        if not isinstance(when_raw, dict) or not when_raw:
            raise ProfileError(f"{where}.when 必须是非空映射（算子 → 阈值）")
        when: dict[str, float] = {}
        for op, value in when_raw.items():
            op_text = str(op).strip()
            if op_text not in RULE_OPS:
                raise ProfileError(
                    f"{where}.when 的算子 {op_text!r} 不支持（可选：{', '.join(RULE_OPS)}）"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProfileError(f"{where}.when.{op_text} 必须是数字，收到 {value!r}")
            when[op_text] = float(value)

        emit = item.get("emit")
        if not isinstance(emit, dict):
            raise ProfileError(f"{where} 缺少 emit（命中后落到哪条关系上）")
        rel_name = str(emit.get("relation") or "").strip()
        target_type = str(emit.get("target_type") or "").strip()
        rel = relations.get(rel_name)
        if rel is None:
            raise ProfileError(f"{where}.emit.relation 引用了未定义关系 {rel_name!r}")
        if target_type not in entities:
            raise ProfileError(f"{where}.emit.target_type 引用了未定义实体 {target_type!r}")
        if not rel.allows_object(target_type):
            raise ProfileError(
                f"{where}.emit.target_type {target_type} 不是关系 {rel_name} 的宾语"
            )
        match_raw = emit.get("target_match")
        if not isinstance(match_raw, dict) or not match_raw:
            raise ProfileError(
                f"{where}.emit.target_match 必须是非空映射（用哪些属性挑出那一个实例）"
            )
        target_entity = entities[target_type]
        for attr in match_raw:
            if attr not in target_entity.attributes:
                raise ProfileError(
                    f"{where}.emit.target_match 引用了未定义属性 {target_type}.{attr}"
                )

        out[rid] = RuleDef(
            id=rid,
            type=rtype,
            key=key,
            when=when,
            relation=rel_name,
            target_type=target_type,
            target_match=dict(match_raw),
            cq=str(item.get("cq") or "").strip(),
            message=str(item.get("message") or "").strip(),
        )
    return out


def load_profile(profile: str | Path, *, register: bool = True) -> OntologyRegistry:
    """加载并校验一个 profile；`extends` 会先加载父 profile 再合并。"""
    path = _resolve(profile)
    data = _read_yaml(path)

    parent: OntologyRegistry | None = None
    extends = data.get("extends")
    if extends:
        parent = load_profile(str(extends), register=False)

    version = int(data.get("version") or 1)
    profile_id = str(data.get("profile_id") or path.stem)
    domain = str(data.get("domain") or "generic")

    entities = dict(parent.entity_types) if parent else {}
    entities.update(_parse_entities(data.get("entity_types"), path))

    relations = dict(parent.relation_types) if parent else {}
    relations.update(_parse_relations(data.get("relation_types") or [], entities, path))

    constraints = dict(parent.constraints) if parent else {}
    constraints.update(_parse_constraints(data.get("constraints"), entities, relations, path))

    rules = dict(parent.rules) if parent else {}
    rules.update(_parse_rules(data.get("rules"), entities, relations, path))

    registry = OntologyRegistry(
        profile_id=profile_id,
        version=version,
        domain=domain,
        entity_types=entities,
        relation_types=relations,
        source_path=path,
        display_name=str(data.get("display_name") or ""),
        description=str(data.get("description") or ""),
        constraints=constraints,
        rules=rules,
    )
    if register:
        registry.register()
    return registry


# ---------------- 缓存 ----------------

_cache: dict[str, tuple[float, OntologyRegistry]] = {}


def get_registry(profile: str | Path | None = None, *, reload: bool = False) -> OntologyRegistry:
    """按文件 mtime 缓存；文件没变就不重复解析与注册。"""
    path = _resolve(profile or DEFAULT_PROFILE)
    key = str(path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    cached = _cache.get(key)
    if cached and not reload and cached[0] == mtime:
        return cached[1]
    registry = load_profile(path)
    _cache[key] = (mtime, registry)
    logger.info("已加载本体 profile %s（%s）", registry.profile_id, path.name)
    return registry


def reset_cache() -> None:
    """测试用：清空 registry 缓存并清掉 schema 的动态注册。"""
    _cache.clear()
    graph_schema.reset_dynamic_types()
