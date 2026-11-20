"""本体约束求值：拿 TBox 里声明的约束去检查 ABox。

**这一层解决的是一类以前查不出来的坏数据。**

举例：应急预案对每一级响应都逐条列出了处置措施（Ⅳ级 5 条、Ⅲ/Ⅱ级各 6 条、Ⅰ级 4 条）。
我们建库时漏建了几条，程序里没有任何东西会报错——直到用户提问、答案拉出来，
人一眼看过去觉得"不完整"。靠人肉发现的缺陷等于没有防线。

约束把它变成**建库那一刻的可检测错误**：少了就报，报了就补。

设计取舍
--------
1. **约束的定义和求值分开**：定义在 `registry._parse_constraints`（写错在加载期炸），
   求值在这里（数据不满足才报）。两类错误要能分清。
2. **不猜领域**：计数只认"某类型的实例沿某关系连了多少条"，不认识"水库""措施"。
   每一条约束都带 `cq` 字段，钉回 `docs/本体能力问题清单.md`——**没有 CQ 支撑的约束不该写**。
3. **只认 active**：archived 的实例和关系不参与求值，否则删过的数据会一直报警。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.graph.ontology.registry import ConstraintDef, OntologyRegistry

#: 严重级排序用（error 排前面）
_SEVERITY_RANK = {"error": 0, "warning": 1}


@dataclass(frozen=True)
class Violation:
    """一条约束违规。"""

    constraint_id: str
    type: str
    severity: str
    entity: str
    #: 出问题的实例名；整类问题（如枚举重复）时是 "(类型)"
    subject: str
    message: str
    cq: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "constraint_id": self.constraint_id,
            "type": self.type,
            "severity": self.severity,
            "entity": self.entity,
            "subject": self.subject,
            "message": self.message,
        }
        if self.cq:
            out["cq"] = self.cq
        if self.detail:
            out["detail"] = dict(self.detail)
        return out

    def describe(self) -> str:
        """一行给人看：`[error] Ⅱ级响应 · response_level.measures_complete (CQ-10) …`"""
        tag = f"[{self.severity}]"
        cq = f" ({self.cq})" if self.cq else ""
        return f"{tag} {self.subject} · {self.constraint_id}{cq} — {self.message}"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """读一个字段。同时兼容 ORM 行和简单的测试替身。"""
    return getattr(obj, name, default)


def _is_active(obj: Any) -> bool:
    return str(_attr(obj, "status", "active") or "active") == "active"


def _as_int(value: Any) -> int | None:
    """把属性值读成整数；读不出来返回 None（不认识的写法一律不当数）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else None


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


class _Box:
    """一次求值用到的索引（实例按 uid、边按出/入方向）。"""

    def __init__(self, instances: Iterable[Any], relations: Iterable[Any]) -> None:
        self.instances: dict[str, Any] = {}
        for inst in instances:
            if not _is_active(inst):
                continue
            uid = str(_attr(inst, "uid", "") or "")
            if uid:
                self.instances[uid] = inst

        self.type_by_uid: dict[str, str] = {
            uid: str(_attr(inst, "entity_type", "") or "") for uid, inst in self.instances.items()
        }
        # 边连同它自己的 props 一起存：`coverage` 要读"我对应原文第几条"，
        # 这个值随主语变（同一条措施在Ⅳ级是第 3 条、在Ⅱ级是第 5 条），只能在边上。
        self.out_edges: dict[str, list[tuple[str, str, Any]]] = {}
        self.in_edges: dict[str, list[tuple[str, str, Any]]] = {}
        for rel in relations:
            if not _is_active(rel):
                continue
            subject = str(_attr(rel, "subject_uid", "") or "")
            obj = str(_attr(rel, "object_uid", "") or "")
            predicate = str(_attr(rel, "predicate", "") or "")
            if not subject or not obj or not predicate:
                continue
            self.out_edges.setdefault(subject, []).append((predicate, obj, rel))
            self.in_edges.setdefault(obj, []).append((predicate, subject, rel))

    def name_of(self, uid: str) -> str:
        inst = self.instances.get(uid)
        return str(_attr(inst, "name", "") or uid) if inst is not None else uid

    def of_type(self, entity_type: str) -> list[Any]:
        rows = [
            inst
            for uid, inst in self.instances.items()
            if self.type_by_uid.get(uid) == entity_type
        ]
        return sorted(rows, key=lambda inst: str(_attr(inst, "name", "") or ""))

    def matching(self, uid: str, constraint: ConstraintDef) -> list[tuple[str, str, Any]]:
        """某实例沿约束指定的关系、指定方向、指定目标类型的边（带边对象）。"""
        rows = (
            self.out_edges.get(uid, [])
            if constraint.direction == "out"
            else self.in_edges.get(uid, [])
        )
        rels = constraint.relation
        out: list[tuple[str, str, Any]] = []
        for predicate, other_uid, rel in rows:
            if rels and predicate not in rels:
                continue
            if constraint.target_type and self.type_by_uid.get(other_uid) != constraint.target_type:
                continue
            out.append((predicate, other_uid, rel))
        return out

    def count(self, uid: str, constraint: ConstraintDef) -> int:
        return len(self.matching(uid, constraint))


def _lower_bound(constraint: ConstraintDef, inst: Any) -> tuple[int | None, bool]:
    """下限从哪来：实例属性优先（每级不一样），TBox 里的固定值兜底。

    返回 (下限, 是否缺了 min_attr 声明的属性)。
    """
    if not constraint.min_attr:
        return constraint.min, False
    props = _attr(inst, "props", None) or {}
    value = _as_int(props.get(constraint.min_attr))
    if value is not None:
        return value, False
    # 声明了 min_attr 却没这个属性：不是"跳过"，而是数据缺了——要报出来。
    return constraint.min, True


def _relation_text(constraint: ConstraintDef) -> str:
    rels = "/".join(constraint.relation) if constraint.relation else "任意关系"
    if constraint.direction == "in":
        rels = "←" + rels
    return rels


def _check_required_attribute(constraint: ConstraintDef, box: _Box) -> list[Violation]:
    out: list[Violation] = []
    for inst in box.of_type(constraint.entity):
        props = _attr(inst, "props", None) or {}
        if _is_blank(props.get(constraint.attribute)):
            name = str(_attr(inst, "name", "") or "")
            out.append(
                Violation(
                    constraint_id=constraint.id,
                    type=constraint.type,
                    severity=constraint.severity,
                    entity=constraint.entity,
                    subject=name,
                    message=constraint.message
                    or f"缺少必填属性 {constraint.attribute}",
                    cq=constraint.cq,
                    detail={"attribute": constraint.attribute},
                )
            )
    return out


def _check_unique_attribute(constraint: ConstraintDef, box: _Box) -> list[Violation]:
    groups: dict[str, list[str]] = {}
    for inst in box.of_type(constraint.entity):
        props = _attr(inst, "props", None) or {}
        value = props.get(constraint.attribute)
        if _is_blank(value):
            continue
        groups.setdefault(str(value), []).append(str(_attr(inst, "name", "") or ""))
    out: list[Violation] = []
    for value, names in sorted(groups.items()):
        if len(names) < 2:
            continue
        out.append(
            Violation(
                constraint_id=constraint.id,
                type=constraint.type,
                severity=constraint.severity,
                entity=constraint.entity,
                subject=" / ".join(names),
                message=constraint.message
                or f"{constraint.attribute}={value} 被 {len(names)} 个实例重复使用",
                cq=constraint.cq,
                detail={"attribute": constraint.attribute, "value": value, "instances": names},
            )
        )
    return out


def _check_count(constraint: ConstraintDef, box: _Box) -> list[Violation]:
    out: list[Violation] = []
    lower_is_count = constraint.type == "min_count"
    for inst in box.of_type(constraint.entity):
        name = str(_attr(inst, "name", "") or "")
        actual = box.count(str(_attr(inst, "uid", "") or ""), constraint)

        if lower_is_count:
            expected, missing_attr = _lower_bound(constraint, inst)
            if missing_attr:
                out.append(
                    Violation(
                        constraint_id=constraint.id,
                        type=constraint.type,
                        severity=constraint.severity,
                        entity=constraint.entity,
                        subject=name,
                        message=(
                            f"缺少 {constraint.min_attr} 属性，无法判断 "
                            f"{_relation_text(constraint)} 的下限"
                        ),
                        cq=constraint.cq,
                        detail={"attribute": constraint.min_attr},
                    )
                )
                continue
            if expected is None:
                # TBox 里没给固定下限、实例上也没写：这条约束对这个实例无话可说。
                continue
            if actual < expected:
                out.append(
                    Violation(
                        constraint_id=constraint.id,
                        type=constraint.type,
                        severity=constraint.severity,
                        entity=constraint.entity,
                        subject=name,
                        message=constraint.message
                        or (
                            f"{_relation_text(constraint)} 只有 {actual} 条，"
                            f"要求至少 {expected} 条"
                        ),
                        cq=constraint.cq,
                        detail={"actual": actual, "expected": expected, "missing": expected - actual},
                    )
                )
            continue

        upper = constraint.max
        if upper is not None and actual > upper:
            out.append(
                Violation(
                    constraint_id=constraint.id,
                    type=constraint.type,
                    severity=constraint.severity,
                    entity=constraint.entity,
                    subject=name,
                    message=constraint.message
                    or f"{_relation_text(constraint)} 有 {actual} 条，最多只能 {upper} 条",
                    cq=constraint.cq,
                    detail={"actual": actual, "expected": upper, "excess": actual - upper},
                )
            )
    return out


def _items_text(items: list[int]) -> str:
    """[1, 3, 4] → `(1)(3)(4)`。原文就是按 (1)(2)(3) 编号的，照抄最好认。"""
    return "".join(f"({n})" for n in items)


def _check_coverage(constraint: ConstraintDef, box: _Box) -> list[Violation]:
    """逐条核对：实例说"原文共 N 条"，每条边说"我对应第几条"，1..N 每一条都要有人认领。

    为什么不满足于 min_count：**数条数分不清"拆分"和"漏建"**。实测Ⅳ级原文 5 条、
    本体连了 6 条看着达标，其实把第 (4) 条拆成两个节点（巡查 / 汇报）凑出了数量，
    同时把第 (2) 条整条漏掉了。逐条核对才分得清。
    """
    out: list[Violation] = []
    for inst in box.of_type(constraint.entity):
        name = str(_attr(inst, "name", "") or "")
        uid = str(_attr(inst, "uid", "") or "")
        props = _attr(inst, "props", None) or {}
        total = _as_int(props.get(constraint.total_attr))
        if total is None:
            out.append(
                Violation(
                    constraint_id=constraint.id,
                    type=constraint.type,
                    severity=constraint.severity,
                    entity=constraint.entity,
                    subject=name,
                    message=(
                        f"缺少 {constraint.total_attr} 属性，无法逐条核对"
                        f"（原文这一级共几条）"
                    ),
                    cq=constraint.cq,
                    detail={"attribute": constraint.total_attr},
                )
            )
            continue

        covered: dict[int, list[str]] = {}
        untagged: list[str] = []
        for _predicate, other_uid, rel in box.matching(uid, constraint):
            edge_props = _attr(rel, "props", None) or {}
            item = _as_int(edge_props.get(constraint.item_attr))
            other_name = box.name_of(other_uid)
            if item is None:
                untagged.append(other_name)
                continue
            covered.setdefault(item, []).append(other_name)

        missing = [n for n in range(1, total + 1) if n not in covered]
        extra = sorted(n for n in covered if n < 1 or n > total)
        if not (missing or extra or untagged):
            continue

        parts: list[str] = []
        if missing:
            parts.append(f"原文 {total} 条中，第 {_items_text(missing)} 条没有对应措施")
        if extra:
            parts.append(f"有措施标注了超出 1..{total} 的条目号 {_items_text(extra)}")
        if untagged:
            parts.append(
                f"{len(untagged)} 条措施没有标注 {constraint.item_attr}"
                f"（{'、'.join(untagged[:5])}{'…' if len(untagged) > 5 else ''}）"
            )
        head = f"{constraint.message}：" if constraint.message else ""
        out.append(
            Violation(
                constraint_id=constraint.id,
                type=constraint.type,
                severity=constraint.severity,
                entity=constraint.entity,
                subject=name,
                message=head + "；".join(parts),
                cq=constraint.cq,
                detail={
                    "total": total,
                    "missing_items": missing,
                    "covered_items": sorted(covered),
                    "untagged": untagged,
                },
            )
        )
    return out


_CHECKERS = {
    "required_attribute": _check_required_attribute,
    "unique_attribute": _check_unique_attribute,
    "min_count": _check_count,
    "max_count": _check_count,
    "coverage": _check_coverage,
}


def check_constraints(
    registry: OntologyRegistry,
    instances: Iterable[Any],
    relations: Iterable[Any],
) -> list[Violation]:
    """按 TBox 约束检查一遍 ABox，返回违规列表（已排序，error 在前）。

    不抛异常：约束不满足是**数据问题**，调用方要的是"报告"而不是"崩溃"。
    只有 TBox 写错（`ProfileError`）才在加载期就炸。
    """
    if not registry.constraints:
        return []
    box = _Box(instances, relations)
    violations: list[Violation] = []
    for constraint in registry.constraints.values():
        checker = _CHECKERS.get(constraint.type)
        if checker is None:  # 解析阶段已经挡掉了，这里只是防御
            continue
        violations.extend(checker(constraint, box))
    violations.sort(key=lambda v: (_SEVERITY_RANK.get(v.severity, 9), v.constraint_id, v.subject))
    return violations


async def check_database(
    db: Any,
    registry: OntologyRegistry,
    *,
    profile_id: str | None = None,
) -> list[Violation]:
    """从 PG 读某个 profile 的 active 实例与关系，跑一遍约束。

    为什么不查图库：PG 是真相源，图只是派生索引。约束要检查的是**定义的数据**，
    拿派生索引去检查，图库没同步时会得出一堆假警报。
    """
    from sqlalchemy import select

    from app.db.models import OntologyInstance, OntologyRelation

    target = profile_id or registry.profile_id
    inst_stmt = select(OntologyInstance).where(
        OntologyInstance.profile_id == target, OntologyInstance.status == "active"
    )
    rel_stmt = select(OntologyRelation).where(
        OntologyRelation.profile_id == target, OntologyRelation.status == "active"
    )
    instances = list((await db.execute(inst_stmt)).scalars().all())
    relations = list((await db.execute(rel_stmt)).scalars().all())
    return check_constraints(registry, instances, relations)


def summarize(violations: Iterable[Violation]) -> dict[str, Any]:
    """给脚本/接口用的汇总。"""
    rows = list(violations)
    by_constraint: dict[str, int] = {}
    for item in rows:
        by_constraint[item.constraint_id] = by_constraint.get(item.constraint_id, 0) + 1
    return {
        "total": len(rows),
        "errors": sum(1 for v in rows if v.severity == "error"),
        "warnings": sum(1 for v in rows if v.severity == "warning"),
        "by_constraint": by_constraint,
    }
