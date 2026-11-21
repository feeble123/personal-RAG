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
#: 带单位的数值：流量 / 水位 / 雨量（只认这几种常见写法，避免把"120 米"误当水位）
#:
#: `m` 后面**不能用 `\b`**：`\b` 是 Unicode 词边界，而汉字也是词字符，所以
#: 「343.2m时」里的 `m` 后面没有边界，整条抽不出来（实测）。改成"后面不是字母
#: 数字"即可——「343.2m时」「343.2m」都命中，`m3/s` 仍由前面的分支先接住。
#:
#: `mm` 必须排在 `m` 前面：`m` 分支的否定断言只能挡住"后面紧跟字母数字"，
#: 排后面时正则先吃到 `m`、剩下一个 `m` 再判失败，虽然最终仍能匹配到 `mm`，
#: 但读起来容易误判，顺序写死更稳。
_VALUE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(m3/s|m³/s|立方米每秒|立方米/秒|mm|毫米|m(?![A-Za-z0-9])|米)"
)

#: 长度单位：这类数值是**高程**，1.7 米的误差会跨过整座坝，不能按百分比宽容。
_LEVEL_UNITS = ("m", "米")
#: 雨量单位（mm 与"毫米"都归成 `mm`）。
_RAIN_UNITS = ("mm", "毫米")
#: 高程匹配的绝对容差（米）。同一个库的特征水位之间通常只差零点几米。
_LEVEL_TOL_M = 0.1
#: 雨量要**看上下文**再认：大坝沉降 20mm、裂缝宽度 15mm 也是 mm 数值，
#: 把它们当雨量会把问题链到错误的工况上。匹配位置往前看这么多字，
#: 出现下面任一个词才算"在说降雨"。
_RAIN_LOOKBEHIND = 16
_RAIN_CUES = ("降雨", "雨量", "降水", "暴雨", "来水", "雨强")

#: 来水年型。「正常年 / 干旱年」是调度规程分两套方案用的词（3.3.1.1 / 3.3.1.2），
#: 同一天的雨量落在哪个年型里，结论可能不同——所以它是一个独立的匹配维度。
_SEASON_RE = re.compile(r"(正常年|干旱年|枯水年|丰水年|平水年)")


@dataclass(frozen=True)
class Condition:
    """一个问题里抽出来的工况条件。"""

    kind: str  # frequency / value / season
    raw: str  # 原文片段，用于给人看
    value: float  # frequency: 超越概率（0.05）；value: 原始数值；season: 0
    unit: str = ""  # 数值条件的单位（m3/s、m、mm）
    return_period: float | None = None
    season: str = ""  # 来水年型（正常年 / 干旱年）

    @property
    def dimension(self) -> str:
        """这条条件归到哪个**匹配维度**（和 TBox 里的属性名对齐）。

        为什么要有维度：同一个问题里可能同时给"雨前库水位 338.5m"和"24h 雨量
        90mm"，而原文的方案是由这两个条件**共同**决定的。没有维度就没法把
        两条条件分别对上实例的两个属性，只能拿每个数去和所有数值属性乱比。
        """
        if self.kind == "frequency":
            return "return_period"
        if self.kind == "season":
            return "season"
        if self.unit in _RAIN_UNITS:
            return "rain_24h_mm"
        if self.unit in _LEVEL_UNITS:
            return "level_m"
        return ""  # 流量等其他量纲暂不参与维度匹配

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "raw": self.raw, "value": self.value}
        if self.unit:
            data["unit"] = self.unit
        if self.return_period is not None:
            data["return_period"] = self.return_period
        if self.season:
            data["season"] = self.season
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
        if unit in _RAIN_UNITS:
            # 见 `_RAIN_CUES`：没有降雨的上下文就不是雨量，宁可不抽。
            before = text[max(0, match.start() - _RAIN_LOOKBEHIND) : match.start()]
            if not any(cue in before for cue in _RAIN_CUES):
                continue
            unit = "mm"
        found.append(
            Condition(kind="value", raw=match.group(0), value=float(match.group(1)), unit=unit)
        )

    for match in _SEASON_RE.finditer(text):
        found.append(
            Condition(kind="season", raw=match.group(0), value=0.0, season=match.group(1))
        )

    return found


#: **重现期**量纲的键（年）；和概率类键分开，避免把 5 年一遇读成 5%
PERIOD_KEYS = ("return_period", "recurrence")
#: 属性里可能表示"频率/重现期"的键
_FREQ_KEYS = ("frequency", "freq", "p", "p_level", "exceedance", *PERIOD_KEYS)
#: 区间工况的上下界键（引擎约定，不是领域词）：
#: 文档里写「20~50 年一遇」，用户只会说「50 年一遇」；声明这两个属性后，
#: 问题的重现期落在 [low, high] 里就算命中。任何本体都能用，不需要改引擎。
INTERVAL_KEYS = ("return_period_low", "return_period_high")
#: 供检索层判断"哪些实体类型算工况"用（声明了频率或区间属性的类型）
FREQUENCY_KEYS: tuple[str, ...] = (*_FREQ_KEYS, *INTERVAL_KEYS)
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
    """数值是否"差不多相等"。

    容差必须**纯相对**：超越概率是 0.05 / 0.02 / 0.01 / 0.001 这种量级。
    - 绝对容差 0.02 会把 0.01 和 0.0033（100 年 vs 300 年一遇）判成相等；
    - 绝对下限 1e-3 同样不行：0.001 与 1/1500≈0.00067 相差 33%，却在这个下限内，
      于是一条「1500 年一遇」的问题会同时命中「50~1000 年一遇」和「1000 年以上」，
      答案里冒出 Ⅱ 级和 Ⅰ 级两个等级（实测）。

    所以下限压到 1e-9，只用来兜底浮点误差，不做"差不多"的宽容。
    """
    return abs(a - b) <= max(1e-9, abs(b) * tol)


def as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _in_interval(props: dict[str, Any], cond: "Condition") -> bool:
    """区间工况：问题的重现期落在 (low, high] 里就算命中。

    为什么是左开右闭：文档写的是「20 年以上 50 年以下」——上界不含、下界含。
    用闭区间的话，20 年一遇会同时落进「10~20」和「20~50」两档，
    一条问题里冒出两个响应等级（Ⅳ 和 Ⅲ），答案自相矛盾（实测踩过）。
    """
    if not cond.return_period:
        return False
    low = as_float(props.get(INTERVAL_KEYS[0]))
    high = as_float(props.get(INTERVAL_KEYS[1]))
    if low is None or high is None:
        return False
    lo, hi = min(low, high), max(low, high)
    return lo + 1e-9 < cond.return_period <= hi + 1e-9


def _compare_holds(actual: float, threshold: float, op: str) -> bool:
    """`actual`（问题里的值）对 `threshold`（实例属性上的阈值）是否满足比较。

    **顺序不能反**：阈值挂在实例上（`level_m: 343.0`），问题里给的是"实际到了
    多少"（343.2）。这里判的是「实际 > 阈值」。
    """
    if op == "gt":
        return actual > threshold
    if op == "gte":
        return actual >= threshold
    if op == "lt":
        return actual < threshold
    if op == "lte":
        return actual <= threshold
    return _close(actual, threshold)


def matches(
    props: dict[str, Any] | None,
    name: str,
    cond: Condition,
    *,
    compares: dict[str, str] | None = None,
) -> bool:
    """这个实例是不是该工况。

    三个来源都看：属性里的频率/重现期、名字里的「20年一遇」、属性/名字里的百分比。
    宁可判"不是"，也不要把别的工况硬套上来（讲错方案的代价比不回答高）。

    `compares` 是「属性名 → 比较语义」，由调用方从 TBox 取（`AttributeDef.compare`）。
    没有它就只能按相等比，而「库水位超过正常蓄水位」那种阈值工况就会被判错。
    """
    props = props or {}
    # 只认**名字**里的工况说法，不去扫任意文本属性（note/description/mode…）。
    # 反例（实测）：某条工况的备注写着「超过下游防护对象防洪标准（20年一遇）」，
    # 扫文本时它就被判成"20 年一遇工况"，把超标准削峰方案混进了 20 年一遇的答案。
    text = name or ""

    if cond.kind == "frequency":
        # 区间工况优先：「20~50 年一遇」这条属性上的区间，能接住用户说的「50 年一遇」
        if _in_interval(props, cond):
            return True
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

            # **按属性名决定量纲**，不要对每个键都三种量纲各比一遍。
            # 反例（实测）：`return_period: 5`（5 年一遇）会被当成「5%」，
            # 于是 20 年一遇（P=5%）把 3~5 年一遇那条工况也拉进了答案。
            if key in PERIOD_KEYS:
                if cond.return_period and _close(number, cond.return_period):
                    return True
                continue

            # 概率类键：可能存概率(0.05)、百分比(5)、甚至重现期(20)，三种都比。
            # 百分比只在数值明显大于 1 时才算——`frequency: 1.0`（1 年一遇=100%）
            # 与「P=1%」的 1.0 长得一样，不加限制 100 年一遇会拉进 1 年一遇。
            if _close(number, cond.value) or (
                number > 1 and _close(number, cond.value * 100)
            ) or (cond.return_period and number > 1 and _close(number, cond.return_period)):
                return True
        periods, percents = _numbers_in_text(text)
        if cond.return_period and any(_close(p, cond.return_period) for p in periods):
            return True
        return any(_close(p / 100.0, cond.value) for p in percents)

    if cond.kind == "value":
        return _value_matches(props, cond, compares)

    return False


def _value_matches(
    props: dict[str, Any], cond: Condition, compares: dict[str, str] | None
) -> bool:
    """一个带单位的数值条件，是否落在实例的某个数值属性上。

    抽出来单放，是因为多条件联合匹配（`matches_all`）判定"阈值型水位"时
    要复用**完全相同**的语义——两处各写一遍，迟早会分叉。
    """
    ops = compares or {}
    is_level = cond.unit in _LEVEL_UNITS
    for key, raw in props.items():
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        op = ops.get(key, "")
        if op:
            # 阈值语义：属性声明了 gt/gte/lt/lte，就问"问题里的值过没过这条线"。
            # 这是「库水位超过正常蓄水位」这类工况唯一说得通的理解方式——
            # 它写的是 343.0，问的却是 343.2。
            if _compare_holds(cond.value, number, op):
                return True
            continue
        # 高程按绝对容差比：343.0 与 343.54 只差 0.54m，用百分比（±1.7m）
        # 会把两个互斥工况同时命中（实测）。流量仍按相对容差。
        if is_level:
            if abs(number - cond.value) <= _LEVEL_TOL_M:
                return True
        elif _close(number, cond.value, tol=0.005):
            return True
    return False


def any_match(
    props: dict[str, Any] | None,
    name: str,
    conditions: Iterable[Condition],
    *,
    compares: dict[str, str] | None = None,
) -> bool:
    return any(matches(props, name, cond, compares=compares) for cond in conditions)


# ---------------- 多条件联合匹配（CQ-04）----------------

#: 问题里可能出现、但实例侧属性名不同的维度。左边是 `Condition.dimension`，
#: 右边是实例上用来判定的属性（可能有多个，按顺序找）。
_RAIN_LOW_KEY = "rain_low_mm"
_RAIN_HIGH_KEY = "rain_high_mm"
_START_LEVEL_KEY = "start_level_m"


def dimension_values(conditions: Iterable[Condition]) -> dict[str, Any]:
    """把问题里的条件按**维度**归拢成 `{维度: 取值}`。

    同一维度出现多次时只留第一次的（「20 年一遇，P=5%」两条说的是同一件事）。
    这条规则也让"引用原文式提问"（「24h降雨量大于85mm且小于100mm」抽到两个数）
    退化成上界，不至于把两条矛盾的值同时拿去比。
    """
    out: dict[str, Any] = {}
    for cond in conditions or ():
        dim = cond.dimension
        if not dim or dim in out:
            continue
        if dim == "return_period":
            if cond.return_period:
                out[dim] = float(cond.return_period)
        elif dim == "season":
            if cond.season:
                out[dim] = cond.season
        else:
            out[dim] = float(cond.value)
    return out


def _has_period_dim(props: dict[str, Any], name: str) -> bool:
    if any(key in props for key in FREQUENCY_KEYS):
        return True
    periods, percents = _numbers_in_text(name or "")
    return bool(periods or percents)


def _period_matches(
    props: dict[str, Any], name: str, years: float, compares: dict[str, str] | None
) -> bool:
    if years <= 0:
        return False
    cond = Condition(
        kind="frequency", raw="", value=round(1.0 / years, 6), return_period=years
    )
    return matches(props, name, cond, compares=compares)


def _rain_in_band(props: dict[str, Any], value: float) -> bool:
    """24h 雨量落在实例声明的 `(low, high]` 区间里。

    左开右闭与重现期区间一致（`_in_interval`）：原文的相邻两档是
    「大于 85mm 且小于 100mm」和「大于 100mm 且小于 110mm」，边界值按
    **上界含、下界不含**归档，才能像 `20 年一遇` 那样只落进一档。
    只有单边的档（「小于 85mm」）缺什么界就不判什么界。
    """
    low = as_float(props.get(_RAIN_LOW_KEY))
    high = as_float(props.get(_RAIN_HIGH_KEY))
    if low is None and high is None:
        return False
    if low is not None and not value > low + 1e-9:
        return False
    if high is not None and not value <= high + 1e-9:
        return False
    return True


def _level_matches(
    props: dict[str, Any], value: float, compares: dict[str, str] | None
) -> bool:
    """库水位维度。

    两种实例各有各的判法，不能混：

    - `start_level_m`（如「正常年工况，雨前水位 338.5m」）是**档位**，按绝对容差
      精确比。不能用 "达到" 的 `>=`：340m 档会顺带命中 341m，两档同时满足，
      又回到 CQ-03 修过的"一个水位落在两个互斥工况里"。
    - `level_m` 带 `compare: gt`（如「库水位超过正常蓄水位」）是**阈值**，
      走原来的阈值语义（实际值 vs 阈值）。
    """
    start = as_float(props.get(_START_LEVEL_KEY))
    if start is not None:
        return abs(start - value) <= _LEVEL_TOL_M
    cond = Condition(kind="value", raw="", value=value, unit="m")
    return _value_matches(props, cond, compares)


def _has_level_dim(props: dict[str, Any], compares: dict[str, str] | None) -> bool:
    if as_float(props.get(_START_LEVEL_KEY)) is not None:
        return True
    # 阈值型水位：TBox 声明了 compare，且属性值读得出来
    return any(as_float(props.get(key)) is not None for key in (compares or {}))


def _season_matches(props: dict[str, Any], season: str) -> bool:
    want = str(props.get("season") or "").strip()
    if not want:
        return False
    return want == str(season).strip()


def matches_all(
    props: dict[str, Any] | None,
    name: str,
    dims: dict[str, Any],
    *,
    compares: dict[str, str] | None = None,
) -> bool:
    """实例是不是问题描述的**整个**工况（多个条件按 AND 合并）。

    为什么要这一步：调度规程里每个方案的前置条件是「当雨前库水位达到 338.5m，
    **且** 预计 24h 降雨量大于 85mm 且小于 100mm 的情况下」——两个维度共同决定
    落到哪一档。只按雨量判，140mm 会同时命中"5~10 年一遇"（120m³/s，正常年）
    和"5~20 年一遇"（130m³/s，干旱年），答案自相矛盾。

    规则：**问题里给出、实例上也声明了的维度，必须全部满足**；
    实例没声明的维度不参与（不因为"它没写"就判不命中）；
    一个问题都没比过（`checked == 0`）也判不命中——宁可不说。
    """
    props = props or {}
    dims = dims or {}
    checked = 0

    years = dims.get("return_period")
    if years is not None and _has_period_dim(props, name):
        if not _period_matches(props, name, float(years), compares):
            return False
        checked += 1

    rain = dims.get("rain_24h_mm")
    if rain is not None:
        if as_float(props.get(_RAIN_LOW_KEY)) is not None or as_float(
            props.get(_RAIN_HIGH_KEY)
        ) is not None:
            if not _rain_in_band(props, float(rain)):
                return False
            checked += 1

    level = dims.get("level_m")
    if level is not None and _has_level_dim(props, compares):
        if not _level_matches(props, float(level), compares):
            return False
        checked += 1

    season = dims.get("season")
    if season and str(props.get("season") or "").strip():
        if not _season_matches(props, str(season)):
            return False
        # **年型只做否决，不计入 `checked`**：它是修饰词，不是工况本身。
        # 计数的话，「正常年 24h 降雨 140mm」会把「300年一遇洪水（正常年）」
        # 也链接进来——那条实例只有重现期和年型，没有雨量维度，靠年型就过关了，
        # 答案里于是混进「超标准洪水削峰方案 450m³/s」（实测）。

    return checked > 0
