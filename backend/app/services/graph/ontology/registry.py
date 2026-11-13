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


class ProfileError(ValueError):
    """profile 文件不合法（消息里带文件与字段位置，便于排查）。"""


@dataclass(frozen=True)
class AttributeDef:
    name: str
    type: str
    required: bool = False
    values: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "type": self.type, "required": self.required}
        if self.values:
            out["values"] = list(self.values)
        if self.description:
            out["description"] = self.description
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
    ) -> None:
        self.profile_id = profile_id
        self.version = version
        self.domain = domain
        self.entity_types = entity_types
        self.relation_types = relation_types
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
            attributes[a_name] = AttributeDef(
                name=a_name,
                type=a_type,
                required=bool(attr.get("required", False)),
                values=values,
                description=str(attr.get("description") or ""),
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
        out[name] = RelationTypeDef(
            name=name,
            source=source,
            target=target,
            description=str(item.get("description") or ""),
            display_name=str(item.get("display_name") or ""),
            target_types=target_types,
            inverse_of=item.get("inverse_of"),
        )
    return out


def _parse_constraints(raw: Any, entities: dict[str, EntityTypeDef], path: Path) -> None:
    for idx, item in enumerate(raw or []):
        if not isinstance(item, dict):
            raise ProfileError(f"{path.name}: constraints[{idx}] 必须是映射")
        entity = str(item.get("entity") or "")
        attr = str(item.get("attribute") or "")
        if item.get("type") == "required_attribute":
            ent = entities.get(entity)
            if ent is None:
                raise ProfileError(f"{path.name}: constraints[{idx}] 引用了未定义实体 {entity!r}")
            if attr and attr not in ent.attributes:
                raise ProfileError(
                    f"{path.name}: constraints[{idx}] 引用了未定义属性 {entity}.{attr}"
                )


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

    _parse_constraints(data.get("constraints"), entities, path)

    registry = OntologyRegistry(
        profile_id=profile_id,
        version=version,
        domain=domain,
        entity_types=entities,
        relation_types=relations,
        source_path=path,
        display_name=str(data.get("display_name") or ""),
        description=str(data.get("description") or ""),
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
