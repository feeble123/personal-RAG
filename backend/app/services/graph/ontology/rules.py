"""本体规则求值：用 TBox 里声明的规则，从"问题里的工况数值"推出该落到哪个实例。

**这一层回答的是「本体到底比 GraphRAG 多了什么」。**

GraphRAG 只能给你**文档里出现过的关系**。所以 100 年一遇能连到 Ⅱ级响应，
是因为建库的人刚好手工连过那一条；300 年一遇没人连，链路就断在那儿。

本体不一样：规则说「重现期落在 (50, 1000] 就归 Ⅱ级响应」，
那么 20 / 50 / 100 / 300 / 1000 全部自动归位，**一条边都不用连**。
新增一档工况，只加一条规则，引擎一行不动。

设计取舍
--------
1. **规则写在 YAML 里**（`rules:` 段），引擎只做求值——领域定义是数据，不是代码。
2. **规则命中的是"问题里的工况"，不是一个实例**。实例可能是区间（「50~1000 年一遇」），
   它代表的范围由 `_link_conditions_by_value` 负责接住；规则负责的是"这个数值该判成哪一类"。
3. **不猜**：数值抽不出来、目标实例找不到，就什么都不产出，绝不硬凑一个结论。
"""
from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from typing import Any, Iterable

from app.services.graph.ontology.conditions import Condition
from app.services.graph.ontology.registry import OntologyRegistry, RuleDef

logger = logging.getLogger(__name__)

_OPERATORS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def value_of(cond: Condition, key: str) -> float | None:
    """从一条工况里取规则要比较的那个数。

    `return_period` 只对"年一遇"类工况有意义（`P=5%` 没有重现期），取不到就是 None，
    调用方据此跳过——不要拿 0 去比，0 会命中一切 `lte` 规则。
    """
    if key == "return_period":
        return cond.return_period
    if key in ("value", "frequency"):
        return cond.value
    raw = getattr(cond, key, None)
    return float(raw) if isinstance(raw, (int, float)) else None


def rule_matches(rule: RuleDef, value: float | None) -> bool:
    """`when` 里的多个条件是 AND。"""
    if value is None:
        return False
    for op, threshold in rule.when.items():
        compare = _OPERATORS.get(op)
        if compare is None:  # 解析阶段已经挡掉，这里防御
            return False
        if not compare(value, threshold):
            return False
    return True


def target_matches(rule: RuleDef, props: dict[str, Any] | None) -> bool:
    """这条规则落到哪一个实例上（如 `{level: Ⅱ}` → Ⅱ级响应）。"""
    props = props or {}
    for key, want in rule.target_match.items():
        if str(props.get(key, "")).strip() != str(want).strip():
            return False
    return True


@dataclass(frozen=True)
class RuleHit:
    """一次规则命中：某条规则被问题里的某个工况满足了。"""

    rule: RuleDef
    condition: Condition
    value: float

    def describe(self) -> str:
        bounds = " 且 ".join(f"{op} {threshold:g}" for op, threshold in self.rule.when.items())
        return f"{self.rule.id}: {self.condition.raw} 的 {self.rule.key}={self.value:g}（{bounds}）"


def matching_rules(
    registry: OntologyRegistry,
    conditions: Iterable[Condition],
) -> list[RuleHit]:
    """所有被满足的规则。纯函数，不碰数据库，便于单测。"""
    if registry is None or not registry.rules:
        return []
    hits: list[RuleHit] = []
    seen: set[tuple[str, str]] = set()
    for cond in conditions:
        for rule in registry.rules.values():
            value = value_of(cond, rule.key)
            if value is None or not rule_matches(rule, value):
                continue
            # 同一个问题里可能抽出两条一模一样的工况（"100 年一遇（P=1%）"），
            # 去重键带上原值，避免同一条规则被同一个数值重复命中两次。
            key = (rule.id, f"{value:g}")
            if key in seen:
                continue
            seen.add(key)
            hits.append(RuleHit(rule=rule, condition=cond, value=value))
    return hits
