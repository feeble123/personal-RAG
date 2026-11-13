"""TBox 在线编辑（F3）：把 `entity_types` / `attributes` 的增删改写回 YAML。

**为什么用 ruamel 而不是 PyYAML**

TBox 文件是给人看、给人改的——`tuning.yaml` 开头上百行都是设计说明。
`yaml.safe_dump` 会把注释和空行全部丢掉，一次保存就毁掉这些说明。
ruamel 的 round-trip 模式能保住注释、键顺序和块风格，只在真正改动的地方产生 diff。

**写入是"要么全对，要么不动"**

每次修改先备份原文本 → 落盘 → 用 registry 的同一套校验重新解析 →
解析失败就把原文本写回去再报错。半截写坏的 TBox 会让整个库打不开，
不能等下次加载才发现。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from app.services.graph.ontology import libraries as lib_mod
from app.services.graph.ontology.registry import ProfileError, get_registry, load_profile

logger = logging.getLogger(__name__)

# 与 registry 的常量保持一致（那边是内网私有常量，这里重新声明以免耦合到实现细节）
ATTRIBUTE_TYPES = ("string", "text", "number", "range", "enum", "list", "boolean")
SCOPES = ("local", "global")


# ---------------- 数据结构 ----------------


@dataclass(frozen=True)
class EntityTypeInput:
    name: str
    display_name: str
    uid_prefix: str
    scope: Literal["local", "global"] = "local"


@dataclass(frozen=True)
class RelationTypeInput:
    name: str
    display_name: str
    source: str
    target: str
    target_types: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class AttributeInput:
    name: str
    type: str = "string"
    values: tuple[str, ...] = ()
    required: bool = False
    description: str = ""


# ---------------- YAML 读写 ----------------


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # 别把长列表折行，diff 会变得没法看
    y.indent(mapping=2, sequence=4, offset=2)  # 保持 `  - name:` 这种现有风格
    return y


def _path(profile_id: str) -> Path:
    path = lib_mod.library_path(profile_id)
    if not path.exists():
        raise ProfileError(f"图谱库 {profile_id!r} 不存在")
    return path


def _load(path: Path) -> CommentedMap:
    doc = _yaml().load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, CommentedMap):
        raise ProfileError(f"{path.name}: 顶层必须是映射")
    if doc.get("extends"):
        raise ProfileError(
            f"{path.name}: 该库通过 extends 继承父本，界面上不支持直接编辑，请手动改 YAML"
        )
    return doc


def _dump(doc: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _commit(profile_id: str, path: Path, doc: CommentedMap) -> None:
    """落盘 + 立即校验 + 失败回滚 + 刷新 registry 缓存。"""
    original = path.read_text(encoding="utf-8")
    path.write_text(_dump(doc), encoding="utf-8", newline="\n")
    try:
        load_profile(path, register=False)
    except Exception as exc:  # noqa: BLE001 - 校验失败要原样恢复
        path.write_text(original, encoding="utf-8", newline="\n")
        raise ProfileError(f"改动未通过校验，已回滚：{exc}") from exc
    get_registry(profile_id, reload=True)
    logger.info("TBox 已更新：%s（%s）", profile_id, path.name)


def _entities(doc: CommentedMap) -> CommentedSeq:
    items = doc.get("entity_types")
    if not isinstance(items, CommentedSeq):
        raise ProfileError("entity_types 必须是列表")
    return items


def _find(seq: CommentedSeq, name: str) -> CommentedMap | None:
    for item in seq:
        if isinstance(item, CommentedMap) and str(item.get("name")) == name:
            return item
    return None


# ---------------- 实体类型 ----------------


def upsert_entity_type(profile_id: str, payload: EntityTypeInput) -> dict[str, Any]:
    """新建或修改一个实体类型。

    `name` 与 `uid_prefix` 是实例 uid 的来源，**改了就等于把老实例全孤立**，
    所以只允许新建时指定；已存在的类型只更新中文名与作用域。
    """
    path = _path(profile_id)
    doc = _load(path)
    seq = _entities(doc)
    existing = _find(seq, payload.name)

    if existing is None:
        if payload.scope not in SCOPES:
            raise ProfileError(f"作用域只能是 {'/'.join(SCOPES)}")
        entry = CommentedMap()
        entry["name"] = payload.name
        entry["display_name"] = payload.display_name
        entry["uid_prefix"] = payload.uid_prefix
        entry["scope"] = payload.scope
        seq.append(entry)
    else:
        if str(existing.get("uid_prefix")) != payload.uid_prefix:
            raise ProfileError(
                f"实体类型 {payload.name!r} 的编号前缀不允许修改（改了老实例的 uid 会对不上）"
            )
        existing["display_name"] = payload.display_name
        existing["scope"] = payload.scope

    _commit(profile_id, path, doc)
    return get_registry(profile_id).to_dict()


def delete_entity_type(profile_id: str, name: str, *, instance_count: int = 0) -> dict[str, Any]:
    """删除实体类型。被关系引用或已有实例时拒绝。"""
    path = _path(profile_id)
    doc = _load(path)
    reg = get_registry(profile_id)
    if name not in reg.entity_types:
        raise ProfileError(f"实体类型 {name!r} 不存在")
    if instance_count:
        raise ProfileError(f"「{reg.display_name(name)}」下还有 {instance_count} 个实例，请先删除实例")

    used_by = [
        r.name for r in reg.relation_types.values()
        if r.source == name or r.target == name or name in (r.target_types or ())
    ]
    if used_by:
        raise ProfileError(
            f"「{reg.display_name(name)}」被关系类型 {', '.join(used_by)} 引用，请先删除这些关系类型"
        )

    seq = _entities(doc)
    entry = _find(seq, name)
    if entry is None:
        raise ProfileError(f"实体类型 {name!r} 不存在")
    seq.remove(entry)
    _commit(profile_id, path, doc)
    return get_registry(profile_id).to_dict()


# ---------------- 关系类型 ----------------


def _relations(doc: CommentedMap) -> CommentedSeq:
    items = doc.get("relation_types")
    if items is None:
        items = CommentedSeq()
        doc["relation_types"] = items
    if not isinstance(items, CommentedSeq):
        raise ProfileError("relation_types 必须是列表")
    return items


def upsert_relation_type(profile_id: str, payload: RelationTypeInput) -> dict[str, Any]:
    """新建或修改一个关系类型。

    `name` 就是边上的 `predicate`，改了等于把已有边全变成「没有类型定义」的孤儿边，
    所以只允许新建时指定；已存在的只更新中文名 / 两端 / 说明。
    """
    path = _path(profile_id)
    doc = _load(path)
    reg = get_registry(profile_id)

    for ref in (payload.source, payload.target, *payload.target_types):
        if ref not in reg.entity_types:
            raise ProfileError(f"关系引用了未定义的实体类型：{ref!r}")

    seq = _relations(doc)
    existing = _find(seq, payload.name)
    if existing is None:
        entry = CommentedMap()
        entry["name"] = payload.name
        entry["display_name"] = payload.display_name
        entry["source"] = payload.source
        entry["target"] = payload.target
        if payload.target_types:
            entry["target_types"] = list(payload.target_types)
        if payload.description:
            entry["description"] = payload.description
        seq.append(entry)
    else:
        existing["display_name"] = payload.display_name
        existing["source"] = payload.source
        existing["target"] = payload.target
        if payload.target_types:
            existing["target_types"] = list(payload.target_types)
        elif "target_types" in existing:
            del existing["target_types"]
        if payload.description:
            existing["description"] = payload.description
        elif "description" in existing:
            del existing["description"]

    _commit(profile_id, path, doc)
    return get_registry(profile_id).to_dict()


def delete_relation_type(
    profile_id: str, name: str, *, edge_count: int = 0
) -> dict[str, Any]:
    """删除关系类型。已经有边在用的时候拒绝——先删边再删类型。"""
    path = _path(profile_id)
    doc = _load(path)
    reg = get_registry(profile_id)
    if name not in reg.relation_types:
        raise ProfileError(f"关系类型 {name!r} 不存在")
    if edge_count:
        raise ProfileError(f"已有 {edge_count} 条关系在用「{name}」，请先删除这些关系")

    seq = _relations(doc)
    entry = _find(seq, name)
    if entry is None:
        raise ProfileError(f"关系类型 {name!r} 不存在")
    seq.remove(entry)
    _commit(profile_id, path, doc)
    return get_registry(profile_id).to_dict()


# ---------------- 属性 ----------------


def upsert_attribute(profile_id: str, entity_name: str, payload: AttributeInput) -> dict[str, Any]:
    path = _path(profile_id)
    doc = _load(path)
    entry = _find(_entities(doc), entity_name)
    if entry is None:
        raise ProfileError(f"实体类型 {entity_name!r} 不存在")

    if payload.type not in ATTRIBUTE_TYPES:
        raise ProfileError(f"属性类型非法：可选 {', '.join(ATTRIBUTE_TYPES)}")
    if payload.type == "enum" and not payload.values:
        raise ProfileError("枚举类型必须给出至少一个可选值")

    attrs = entry.get("attributes")
    if not isinstance(attrs, CommentedSeq):
        attrs = CommentedSeq()
        entry["attributes"] = attrs

    existing = _find(attrs, payload.name)
    if existing is None:
        existing = CommentedMap()
        attrs.append(existing)
    existing["name"] = payload.name
    existing["type"] = payload.type
    if payload.values:
        existing["values"] = list(payload.values)
    elif "values" in existing:
        del existing["values"]
    if payload.required:
        existing["required"] = True
    elif "required" in existing:
        del existing["required"]
    if payload.description:
        existing["description"] = payload.description
    elif "description" in existing:
        del existing["description"]

    _commit(profile_id, path, doc)
    return get_registry(profile_id).to_dict()


def delete_attribute(profile_id: str, entity_name: str, attr_name: str) -> dict[str, Any]:
    path = _path(profile_id)
    doc = _load(path)
    entry = _find(_entities(doc), entity_name)
    if entry is None:
        raise ProfileError(f"实体类型 {entity_name!r} 不存在")
    attrs = entry.get("attributes")
    if not isinstance(attrs, CommentedSeq):
        raise ProfileError(f"{entity_name} 没有属性")
    target = _find(attrs, attr_name)
    if target is None:
        raise ProfileError(f"属性 {attr_name!r} 不存在")
    if target.get("required"):
        raise ProfileError(f"属性 {attr_name!r} 是必填项，直接删会让已有实例不合规；请先取消必填")
    attrs.remove(target)
    _commit(profile_id, path, doc)
    return get_registry(profile_id).to_dict()
