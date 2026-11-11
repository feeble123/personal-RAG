"""本体实例校验：把一条待写入的事实对照 TBox 检查。

设计取舍
--------
- **逐条告警、不中断整批**：一条坏数据不该让整张参数表导入失败。
- **错误 vs 告警**：类型/必填/枚举/端点不合格 = 错误（拒绝写入）；
  出现 TBox 未声明的属性 = 告警（允许写入，但提示补充定义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.graph.ontology.registry import OntologyRegistry


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationResult") -> "ValidationResult":
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self


def _check_attribute(name: str, attr_type: str, values: tuple[str, ...], value: Any) -> str | None:
    """返回错误消息或 None。"""
    if value is None or value == "":
        return f"属性 {name} 为空"
    if attr_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"属性 {name} 需要数字，收到 {value!r}"
    elif attr_type == "boolean":
        if not isinstance(value, bool):
            return f"属性 {name} 需要布尔值，收到 {value!r}"
    elif attr_type == "enum":
        if str(value) not in values:
            return f"属性 {name} 取值 {value!r} 不在枚举 {list(values)} 内"
    elif attr_type == "list":
        if not isinstance(value, (list, tuple)):
            return f"属性 {name} 需要列表，收到 {type(value).__name__}"
    elif attr_type == "range":
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)
        ):
            return f"属性 {name} 需要 [下限, 上限] 两个数字，收到 {value!r}"
    else:  # string / text
        if not isinstance(value, str):
            return f"属性 {name} 需要字符串，收到 {type(value).__name__}"
    return None


def validate_entity(
    registry: OntologyRegistry,
    entity_type: str,
    name: str,
    props: dict[str, Any] | None = None,
) -> ValidationResult:
    """校验一个实体实例（类型、名称、属性）。"""
    result = ValidationResult()
    ent = registry.entity(entity_type)
    if ent is None:
        result.errors.append(
            f"未定义实体类型 {entity_type!r}；可用：{', '.join(sorted(registry.entity_types))}"
        )
        return result
    if not (name or "").strip():
        result.errors.append(f"{entity_type} 实例缺少名称")

    props = props or {}
    for attr_name, attr in ent.attributes.items():
        if attr_name not in props:
            if attr.required:
                result.errors.append(f"{entity_type}.{name}: 缺少必填属性 {attr_name}")
            continue
        err = _check_attribute(attr_name, attr.type, attr.values, props[attr_name])
        if err:
            result.errors.append(f"{entity_type}.{name}: {err}")

    unknown = [k for k in props if k not in ent.attributes]
    for key in unknown:
        result.warnings.append(f"{entity_type}.{name}: 属性 {key} 未在 TBox 中声明")
    return result


def validate_relation(
    registry: OntologyRegistry,
    relation: str,
    subject_type: str,
    object_type: str,
) -> ValidationResult:
    """校验一条关系（关系是否声明、端点是否合法）。"""
    result = ValidationResult()
    rel = registry.relation(relation)
    if rel is None:
        result.errors.append(
            f"未定义关系类型 {relation!r}；可用：{', '.join(sorted(registry.relation_types))}"
        )
        return result
    if not registry.endpoints_ok(relation, subject_type, object_type):
        allowed = rel.target_types or (rel.target,)
        result.errors.append(
            f"{relation} 端点非法：{subject_type} -> {object_type}；"
            f"应为主语 {rel.source}、宾语 {'/'.join(allowed)}"
        )
    return result
