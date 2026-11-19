"""工况条件抽取与匹配（`ontology/conditions.py`）。

这块直接决定"讲的是 20 年一遇还是 100 年一遇的方案"，判错比不判更糟——
所以既要认得出常见说法，也要在拿不准时判"不是"。
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import conditions as C


class TestExtract:
    def test_阿拉伯数字年一遇(self):
        [cond] = C.extract_conditions("青溪水库遇到20年一遇的洪水怎么办")
        assert cond.kind == "frequency"
        assert cond.return_period == 20
        assert cond.value == pytest.approx(0.05)
        assert cond.raw == "20年一遇"

    @pytest.mark.parametrize(
        "text,expected", [("百年一遇", 100), ("五十年一遇", 50), ("二十年一遇", 20), ("千年一遇", 1000)]
    )
    def test_中文数字年一遇(self, text, expected):
        [cond] = C.extract_conditions(f"遇到{text}的洪水")
        assert cond.return_period == expected
        assert cond.value == pytest.approx(1 / expected, rel=1e-6)

    def test_百分比写法(self):
        [cond] = C.extract_conditions("P=5% 的设计洪水")
        assert cond.kind == "frequency"
        assert cond.value == pytest.approx(0.05)

    def test_频率百分比不带P(self):
        [cond] = C.extract_conditions("频率 1% 的洪峰")
        assert cond.value == pytest.approx(0.01)

    def test_重现期写法(self):
        [cond] = C.extract_conditions("重现期 50 年")
        assert cond.return_period == 50

    def test_年一遇优先于裸百分比(self):
        conds = C.extract_conditions("20年一遇，P=5%")
        assert len(conds) == 1
        assert conds[0].return_period == 20

    def test_带单位的数值(self):
        conds = C.extract_conditions("下泄流量 3200 m3/s，库水位 152.3 m")
        values = {c.value: c.unit for c in conds if c.kind == "value"}
        assert values[3200.0] == "m3/s"
        assert values[152.3] == "m"

    def test_普通问题抽不出条件(self):
        assert C.extract_conditions("自由水蓄水容量的取值范围是多少") == []

    def test_描述给人看(self):
        [cond] = C.extract_conditions("20年一遇")
        assert "20年一遇" in cond.describe() and "5.0%" in cond.describe()


class TestMatch:
    def test_按属性里的概率匹配(self):
        cond = C.extract_conditions("20年一遇")[0]
        assert C.matches({"frequency": 0.05}, "设计工况", cond) is True
        assert C.matches({"frequency": 0.01}, "设计工况", cond) is False

    def test_属性存百分比也认(self):
        cond = C.extract_conditions("20年一遇")[0]
        assert C.matches({"frequency": "5%"}, "设计工况", cond) is True
        assert C.matches({"p_level": 5}, "设计工况", cond) is True

    def test_属性存重现期也认(self):
        cond = C.extract_conditions("20年一遇")[0]
        assert C.matches({"return_period": 20}, "设计工况", cond) is True

    def test_名字里写着年一遇(self):
        cond = C.extract_conditions("20年一遇")[0]
        assert C.matches({}, "20年一遇洪水", cond) is True
        assert C.matches({}, "100年一遇洪水", cond) is False

    def test_名字里写着百分比(self):
        cond = C.extract_conditions("5%")[0]
        assert C.matches({}, "P=5% 工况", cond) is True

    def test_数值条件匹配属性值(self):
        cond = C.extract_conditions("下泄流量 3200 m3/s")[0]
        assert C.matches({"safe_discharge_m3s": 3200}, "溢洪道", cond) is True
        assert C.matches({"safe_discharge_m3s": 3500}, "溢洪道", cond) is False

    def test_没有任何线索时判不是(self):
        cond = C.extract_conditions("20年一遇")[0]
        assert C.matches({}, "青溪水库", cond) is False


class TestIntervalBuckets:
    """区间工况：文档写「20 年以上 50 年以下」，用户只说「50 年一遇」。"""

    def _c(self, years: float) -> Condition:
        return C.Condition(
            kind="frequency", raw=f"{years:g}年一遇",
            value=round(1.0 / years, 6), return_period=years,
        )

    def test_50年一遇落在20到50档(self):
        props = {"return_period_low": 20, "return_period_high": 50}
        assert C.matches(props, "20~50年一遇降雨（170~210mm）", self._c(50))

    def test_20年一遇只落在10到20档(self):
        """左开右闭：20 年一遇属于 10~20，不属于 20~50。

        闭区间会让一条问题里同时冒出 Ⅳ 级和 Ⅲ 级两个响应，答案自相矛盾。
        """
        assert C.matches(
            {"return_period_low": 10, "return_period_high": 20}, "10~20年一遇降雨", self._c(20)
        )
        assert not C.matches(
            {"return_period_low": 20, "return_period_high": 50}, "20~50年一遇降雨", self._c(20)
        )

    def test_重现期不会被当成百分比(self):
        """`return_period: 5` 是 5 年一遇，不是 5%；否则 20 年一遇（P=5%）会误命中。"""
        assert not C.matches({"return_period": 5}, "3~5年一遇降雨", self._c(20))
        assert not C.matches({"frequency": 0.2}, "3~5年一遇降雨", self._c(20))

    def test_备注里的工况字样不参与匹配(self):
        """备注写着「防洪标准（20 年一遇）」，不代表这条实例就是 20 年一遇工况。"""
        props = {"note": "超过下游防护对象防洪标准（20年一遇），转以 480m³/s 削峰"}
        assert not C.matches(props, "超下游标准洪水", self._c(20))
