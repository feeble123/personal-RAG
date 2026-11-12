"""参数推荐卡：把本体实例变成「能直接给 agent/人看」的结构化建议。

规则（与设计文档一致，顺序不能乱）：

1. **先看有没有依据**：没有证据的数值一律不输出（返回 None），只给提示。
   这是本项目的不变量——宁可让 agent 拿不到数，也不给一个编出来的数。
2. **条件收窄优先于通用范围**：命中 `ranges` 里全部条件 → `range_level=conditional`。
3. **多条同时命中且不一致 → 冲突**：并列展示，不替调用方挑一个（`range_level=conflict`）。
4. 没有命中条件 → 退回通用范围（`range_level=general`）。
5. 什么范围都没有 → `range_level=none`，数值字段为空。

置信度是**启发式的**，只用来排序和提示，不代表统计意义：
    无条件但有依据 0.60 / 条件收窄 0.80 / 冲突 0.50 / 无依据 0.00
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyInstance
from app.services.graph.ontology import instances as inst_mod

LEVEL_NONE = "none"
LEVEL_GENERAL = "general"
LEVEL_CONDITIONAL = "conditional"
LEVEL_CONFLICT = "conflict"

# 证据强度：能否点回原文
EVIDENCE_NONE = "none"          # 没有证据
EVIDENCE_CLAIM = "claim"        # 只有一句话说明，找不到出处在哪
EVIDENCE_DOC = "doc_level"      # 定位到文档，但没有具体切片/页码
EVIDENCE_TRACEABLE = "traceable"  # 能定位到具体切片，可点回原文

_CONFIDENCE = {
    LEVEL_NONE: 0.0,
    LEVEL_GENERAL: 0.60,
    LEVEL_CONDITIONAL: 0.80,
    LEVEL_CONFLICT: 0.50,
}

# 依据类型里出现这些词，说明是通用资料而非本流域率定结果
_GENERIC_EVIDENCE_HINTS = ("教材", "手册", "规范", "文献")


@dataclass
class ParameterCard:
    parameter: str
    name: str
    group: str
    unit: str
    range_level: str
    confidence: float
    evidence_level: str = EVIDENCE_NONE
    range: list[float] | None = None
    physical_range: list[float] | None = None
    initial: float | None = None
    sensitivity: str | None = None
    narrowed_by: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "name": self.name,
            "group": self.group,
            "unit": self.unit,
            "range_level": self.range_level,
            "confidence": self.confidence,
            "evidence_level": self.evidence_level,
            "range": self.range,
            "physical_range": self.physical_range,
            "initial": self.initial,
            "sensitivity": self.sensitivity,
            "narrowed_by": list(self.narrowed_by),
            "candidates": list(self.candidates),
            "evidence": list(self.evidence),
            "notes": list(self.notes),
        }


def _pair(low: Any, high: Any) -> list[float] | None:
    if low is None and high is None:
        return None
    return [low, high]


def _condition_matches(when: dict[str, Any], conditions: dict[str, str]) -> bool:
    """条件全部命中才算匹配；问题里没给的条件键视为"未限定"，不匹配。"""
    if not when or not conditions:
        return False
    for key, expected in when.items():
        actual = conditions.get(key)
        if actual is None or str(actual).strip() != str(expected).strip():
            return False
    return True


def _describe(when: dict[str, Any]) -> str:
    return "；".join(f"{k}={v}" for k, v in when.items())


def _evidence_level(items: list[dict[str, Any]]) -> str:
    """判定证据强度：能点回切片 > 只到文档 > 只有文字说明 > 没有。"""
    dicts = [i for i in items if isinstance(i, dict)]
    if not dicts:
        return EVIDENCE_CLAIM
    if any(i.get("chunk_id") or i.get("traceable") for i in dicts):
        return EVIDENCE_TRACEABLE
    if any(i.get("doc_id") for i in dicts):
        return EVIDENCE_DOC
    return EVIDENCE_CLAIM


def build_card(row: OntologyInstance, conditions: dict[str, str] | None = None) -> ParameterCard:
    """单条实例 → 推荐卡（纯函数，好测）。"""
    conditions = {k: v for k, v in (conditions or {}).items() if v not in (None, "")}
    props = row.props or {}
    evidence = list(row.evidence or [])

    card = ParameterCard(
        parameter=row.name,
        name=str(props.get("display_zh") or row.name),
        group=str(props.get("group") or ""),
        unit=str(props.get("unit") or ""),
        range_level=LEVEL_NONE,
        confidence=0.0,
        sensitivity=props.get("sensitivity"),
        evidence=evidence,
    )

    # 不变量：没有依据就不输出任何数值
    if not evidence:
        card.evidence_level = EVIDENCE_NONE
        card.notes.append("没有依据（evidence 为空），按规则不输出数值")
        return card

    card.evidence_level = _evidence_level(evidence)
    if card.evidence_level == EVIDENCE_CLAIM:
        card.notes.append("依据只有文字说明，未能定位到知识库里的原文，暂不可溯源")
    elif card.evidence_level == EVIDENCE_DOC:
        card.notes.append("依据已定位到文档，但没到具体页码/切片，建议补充原文摘录")

    physical = _pair(props.get("physical_low"), props.get("physical_high"))
    general = _pair(props.get("range_low"), props.get("range_high"))

    matched = [
        item
        for item in (row.ranges or [])
        if isinstance(item, dict) and _condition_matches(item.get("when") or {}, conditions)
    ]

    if len(matched) > 1:
        distinct = {(m.get("low"), m.get("high")) for m in matched}
        if len(distinct) > 1:
            card.range_level = LEVEL_CONFLICT
            card.candidates = [
                {"when": m.get("when") or {}, "range": [m.get("low"), m.get("high")]}
                for m in matched
            ]
            card.narrowed_by = [_describe(m.get("when") or {}) for m in matched]
            card.notes.append("多个条件同时命中且取值范围不一致，请人工选择，不自动取舍")
        else:
            card.range_level = LEVEL_CONDITIONAL
            card.range = [matched[0].get("low"), matched[0].get("high")]
            card.narrowed_by = [_describe(m.get("when") or {}) for m in matched]
    elif len(matched) == 1:
        card.range_level = LEVEL_CONDITIONAL
        card.range = [matched[0].get("low"), matched[0].get("high")]
        card.narrowed_by = [_describe(matched[0].get("when") or {})]
    elif general is not None:
        card.range_level = LEVEL_GENERAL
        card.range = general
        if conditions:
            card.notes.append("给定条件没有匹配的收窄范围，返回通用范围")
    else:
        card.notes.append("实例里没有通用范围（range_low/range_high 为空）")

    card.physical_range = physical
    card.initial = props.get("initial")
    card.confidence = _CONFIDENCE.get(card.range_level, 0.0)

    types = {str(e.get("type") or "") for e in evidence if isinstance(e, dict)}
    if types and all(any(hint in t for hint in _GENERIC_EVIDENCE_HINTS) for t in types):
        card.notes.append("依据来自通用教材/手册，未按本流域率定，建议复核")
    return card


async def recommend(
    session: AsyncSession,
    *,
    profile_id: str,
    names: Iterable[str] | None = None,
    conditions: dict[str, str] | None = None,
    kb_id: int = 0,
    limit: int = 50,
) -> list[ParameterCard]:
    """按参数名/条件查推荐卡。同名实例优先取本库专属，其次全局。"""
    rows = await inst_mod.list_instances(
        session, profile_id=profile_id, entity_type="Parameter", status="active", limit=limit * 2
    )
    wanted = {str(n).strip() for n in (names or []) if str(n).strip()}

    chosen: dict[str, OntologyInstance] = {}
    for row in rows:
        if wanted and row.name not in wanted:
            continue
        if row.kb_id not in (0, kb_id):
            continue
        current = chosen.get(row.name)
        # 本库专属（kb_id>0）覆盖全局（kb_id=0）
        if current is None or (row.kb_id == kb_id and current.kb_id == 0):
            chosen[row.name] = row

    cards = [build_card(row, conditions) for row in chosen.values()]
    cards.sort(key=lambda c: (-c.confidence, c.parameter))
    return cards[:limit]
