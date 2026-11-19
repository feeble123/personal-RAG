"""从问题里抽"工况条件"，并判断本体实例是不是这个工况。

为什么要单独一层：同一座水库，20 年一遇和 100 年一遇的调度动作完全不同
（开几孔、开度多少、提前几小时泄）。链路问答必须能认出"问的是哪个工况"，
否则会把 5% 的方案当成 1% 的方案讲。

只做**规则式**抽取，不引模型：这类说法很固定（年一遇 / P=x% / 具体数值+单位），
规则可解释、可测、出错容易定位。抽不到就返回空，不影响主链路。

约定：
- 年一遇说法的**重现期**记在 `return_period`，对应的超越概率 `value = 1/return_period`
  （20 年一遇 → 0.05）；两者都保留，因为不同规程写法不同。
- 数值条件（3200 m³/s、152.3 m）保留原值与单位。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

#: 中文数字 → 阿拉伯数字（够用即可；"百年一遇"这类必须认得出）
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

#: 「20年一遇」「五十年一遇」「百年一遇」
_RETURN_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万]+)\s*年\s*一\s*遇")
#: 「P=5%」「频率 5%」「5% 频率」「重现期 20 年」
_PERCENT_RE = re.compile(r"(?:[Pp]\s*[=＝]\s*)?([0-9]+(?:\.[0-9]+)?)\s*%")
_REPERIOD_RE = re.compile(r"重现期\s*([0-9]+(?:\.[0-9]+)?)\s*年")
#: 带单位的数值：流量 / 水位（只认这几种常见写法，避免把"120 米"误当水位）
_VALUE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(m3/s|m³/s|立方米每秒|立方米/秒|m\b|米)"
)


@dataclass(frozen=True)
class Condition:
    """一个问题里抽出来的工况条件。"""

    kind: str  # frequency / value
    raw: str  # 原文片段，用于给人看
    value: float  # frequency: 超越概率（0.05）；value: 原始数值
    unit: str = ""  # 数值条件的单位（m3/s、m）
    return_period: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "raw": self.raw, "value": self.value}
        if self.unit:
            data["unit"] = self.unit
        if self.return_period is not None:
            data["return_period"] = self.return_period
        return data

    def describe(self) -> str:
        if self.kind == "frequency" and self.return_period:
            return f"{self.raw}（P≈{self.value * 100:.1f}%）"
        if self.kind == "value":
            return self.raw  # 原文已经含数值和单位，别重复一遍
        return self.raw


def cn_to_number(text: str) -> float | None:
    """中文数字转阿拉伯数字（支持到万位；"二十"→20、"百"→100）。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit() or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        return float(text)
    total = 0
    section = 0
    digit = 0
    for ch in text:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + (digit or 1) * 1000) * unit
                total += section
                section = 0
            else:
                section += (digit or 1) * unit
            digit = 0
        else:
            return None
    return float(total + section + digit)


def extract_conditions(question: str) -> list[Condition]:
    """抽工况条件。同一个问题可能同时带频率和数值（罕见但对齐时要都用上）。"""
    text = question or ""
    found: list[Condition] = []

    for match in _RETURN_RE.finditer(text):
        years = cn_to_number(match.group(1))
        if not years or years <= 0:
            continue
        found.append(
            Condition(
                kind="frequency",
                raw=match.group(0),
                value=round(1.0 / years, 6),
                return_period=years,
            )
        )

    if not any(c.kind == "frequency" for c in found):
        for match in _REPERIOD_RE.finditer(text):
            years = float(match.group(1))
            if years > 0:
                found.append(
                    Condition(
                        kind="frequency",
                        raw=match.group(0),
                        value=round(1.0 / years, 6),
                        return_period=years,
                    )
                )
        for match in _PERCENT_RE.finditer(text):
            percent = float(match.group(1))
            if 0 < percent < 100:
                found.append(
                    Condition(kind="frequency", raw=match.group(0), value=percent / 100.0)
                )

    for match in _VALUE_RE.finditer(text):
        unit = match.group(2).replace("³", "3")
        found.append(
            Condition(kind="value", raw=match.group(0), value=float(match.group(1)), unit=unit)
        )

    return found


#: 属性里可能表示"频率/重现期"的键
_FREQ_KEYS = ("frequency", "freq", "p", "p_level", "exceedance", "return_period", "recurrence")
#: 文本里的「20年一遇」「5%」
_FREQ_TEXT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万]+)\s*年\s*一\s*遇")
_PERCENT_TEXT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")


def _numbers_in_text(text: str) -> tuple[set[float], set[float]]:
    """文本里出现的 (重现期集合, 百分比集合)。"""
    periods = set()
    for match in _FREQ_TEXT_RE.finditer(text):
        value = cn_to_number(match.group(1))
        if value:
            periods.add(value)
    percents = {float(m.group(1)) for m in _PERCENT_TEXT_RE.finditer(text)}
    return periods, percents


def _close(a: float, b: float, tol: float = 0.02) -> bool:
    return abs(a - b) <= max(tol, abs(b) * tol)


def matches(props: dict[str, Any] | None, name: str, cond: Condition) -> bool:
    """这个实例是不是该工况。

    三个来源都看：属性里的频率/重现期、名字里的「20年一遇」、属性/名字里的百分比。
    宁可判"不是"，也不要把别的工况硬套上来（讲错方案的代价比不回答高）。
    """
    props = props or {}
    text = " ".join(str(v) for v in props.values() if isinstance(v, (str, int, float)))
    text = f"{name or ''} {text}"

    if cond.kind == "frequency":
        for key in _FREQ_KEYS:
            if key not in props:
                continue
            raw = props[key]
            try:
                number = float(str(raw).strip().rstrip("%"))
            except (TypeError, ValueError):
                continue
            if str(raw).strip().endswith("%"):
                number /= 100.0
            # 同一个键可能存概率(0.05)、百分比(5)、重现期(20)，三种都比一遍
            if _close(number, cond.value) or _close(number, cond.value * 100) or (
                cond.return_period and _close(number, cond.return_period)
            ):
                return True
        periods, percents = _numbers_in_text(text)
        if cond.return_period and any(_close(p, cond.return_period) for p in periods):
            return True
        return any(_close(p / 100.0, cond.value) for p in percents)

    if cond.kind == "value":
        for raw in props.values():
            try:
                number = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if _close(number, cond.value, tol=0.005):
                return True
        return False

    return False


def any_match(props: dict[str, Any] | None, name: str, conditions: Iterable[Condition]) -> bool:
    return any(matches(props, name, cond) for cond in conditions)
