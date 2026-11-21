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


class TestSmallProbabilityTolerance:
    """小概率工况之间的区分。

    踩过的坑：容差带绝对下限 1e-3，结果 0.001（1000 年一遇）和 1/1500≈0.00067
    被判成相等——一条「1500 年一遇」的问题同时命中「50~1000 年一遇」和
    「1000 年以上」，答案里冒出 Ⅱ 级和 Ⅰ 级两个等级。
    """

    def test_一千年和一千五百年一遇不是同一档(self):
        from app.services.graph.ontology.conditions import _close

        assert not _close(0.001, 1 / 1500), "1000 年一遇的概率不该等于 1500 年一遇"

    def test_概率完全相同仍然判等(self):
        from app.services.graph.ontology.conditions import _close

        assert _close(0.01, 1 / 100)

    def test_同一档位内的百分比写法仍能对上(self):
        from app.services.graph.ontology.conditions import _close

        # 文档里写 0.333，实际是 1/3
        assert _close(0.333, 1 / 3)


class TestBoundaryOwnership:
    """边界归属唯一：一个重现期只能落到一个等级。"""

    def _levels(self):
        return {"Ⅳ": (0, 20), "Ⅲ": (20, 50), "Ⅱ": (50, 1000), "Ⅰ": (1000, 10**9)}

    def test_每个重现期只落一档(self):
        for years in (1, 10, 20, 21, 50, 51, 100, 300, 500, 1000, 1001, 1500, 5000):
            hits = [
                level
                for level, (low, high) in self._levels().items()
                if low < years <= high
            ]
            assert len(hits) == 1, f"{years} 年一遇落到了 {hits}"


class TestWaterLevel:
    """水位判别（CQ-03）。三处都错过，逐个守住：

    1. `m\b` 遇中文失效——「343.2m时」整条抽不出来；
    2. 高程用百分比容差（±1.7m），343.0 和 343.54 两个互斥工况同时命中；
    3. 「超过正常蓄水位」写的是 343.0，是**阈值**不是"等于"，按相等永远对不上。
    """

    def test_汉字紧跟单位也能抽出来(self):
        """`\b` 是 Unicode 词边界，汉字也是词字符——「343.2m时」就没有边界。"""
        [cond] = C.extract_conditions("库水位343.2m时适用哪套调度规则")
        assert cond.kind == "value"
        assert cond.value == pytest.approx(343.2)
        assert cond.unit == "m"

    def test_单位后面是字母数字时不误判(self):
        [cond] = C.extract_conditions("下泄 343.2m3/s")
        assert cond.unit == "m3/s"

    def test_高程按绝对容差而不是百分比(self):
        """343.0 与 343.54 只差 0.54m；百分比容差会放宽到 ±1.7m。"""
        cond = C.extract_conditions("库水位343.54米")[0]
        assert C.matches({"level_m": 343.54}, "x", cond) is True   # 同一个高程
        assert C.matches({"level_m": 343.0}, "x", cond) is False   # 差 0.54m → 不算同一个
        # 反证：按百分比容差（343×0.5%≈±1.7m）这里会被判成"同一个"，
        # 于是 343.2m 同时命中 343.0 和 343.54 两个互斥工况。

    def test_阈值语义_超过才算命中(self):
        cond = C.extract_conditions("库水位343.2m")[0]
        compares = {"level_m": "gt"}
        assert C.matches({"level_m": 343.0}, "库水位超过正常蓄水位", cond, compares=compares) is True
        assert C.matches({"level_m": 343.54}, "库水位超过校核洪水位", cond, compares=compares) is False

    def test_没声明阈值时退化成相等(self):
        """TBox 没说 compare 的数值属性仍按"差不多相等"比——不能擅自改成大于。"""
        cond = C.extract_conditions("下泄流量 3200 m3/s")[0]
        assert C.matches({"outflow_m3s": 3200}, "方案", cond) is True
        assert C.matches({"outflow_m3s": 3000}, "方案", cond) is False

    def test_超过两条线时两条都命中(self):
        cond = C.extract_conditions("库水位343.6米")[0]
        compares = {"level_m": "gt"}
        assert C.matches({"level_m": 343.0}, "x", cond, compares=compares) is True
        assert C.matches({"level_m": 343.54}, "x", cond, compares=compares) is True


class TestRainAndSeason:
    """雨量与来水年型两个新维度（CQ-04）。

    原文的调度方案是「当雨前库水位达到 338.5m，且 24h 降雨量大于 85mm 且
    小于 100mm 的情况下」——雨量必须**看上下文**才认，年型决定用哪一套方案
    （正常年 130~155mm 是 120m³/s，干旱年 130~170mm 是 130m³/s）。
    """

    def test_雨量带上下文才认(self):
        [cond] = C.extract_conditions("24h降雨量 90mm 该按哪个方案控泄")
        assert cond.kind == "value"
        assert cond.unit == "mm"
        assert cond.dimension == "rain_24h_mm"

    def test_毫米写法也认(self):
        [cond] = C.extract_conditions("预计 24 小时雨量 140 毫米")
        assert cond.unit == "mm"
        assert cond.value == pytest.approx(140)

    def test_没有降雨上下文不认(self):
        """大坝沉降 20mm、裂缝宽度 15mm 也是 mm——当雨量会把问题链到错工况。"""
        assert C.extract_conditions("大坝沉降 20mm，裂缝宽度 15mm") == []

    def test_年型抽得出来(self):
        conds = C.extract_conditions("干旱年 24h 降雨 140mm 怎么调度")
        seasons = [c for c in conds if c.kind == "season"]
        assert seasons and seasons[0].season == "干旱年"

    def test_维度区分水位与雨量(self):
        conds = C.extract_conditions("雨前库水位 338.5m，24h 降雨量 90mm")
        dims = C.dimension_values(conds)
        assert dims == {"level_m": 338.5, "rain_24h_mm": 90.0}

    def test_同一维度只留第一个(self):
        """「20年一遇，P=5%」说的是同一件事，不该变成两个重现期。"""
        dims = C.dimension_values(C.extract_conditions("20年一遇（P=5%）"))
        assert dims["return_period"] == 20


class TestMatchesAll:
    """多条件按 AND 合并（CQ-04）。"""

    NORMAL = {
        "season": "正常年",
        "rain_low_mm": 85,
        "rain_high_mm": 100,
        "start_level_m": 338.5,
        "return_period": 2,
        "return_period_low": 1,
        "return_period_high": 2,
    }

    def _dims(self, question: str):
        return C.dimension_values(C.extract_conditions(question))

    def test_雨量落在区间里就命中(self):
        assert C.matches_all(self.NORMAL, "x", self._dims("降雨 90mm")) is True

    def test_雨量超出区间不命中(self):
        assert C.matches_all(self.NORMAL, "x", self._dims("降雨 140mm")) is False

    def test_左开右闭_下界不含上界含(self):
        assert C.matches_all(self.NORMAL, "x", self._dims("降雨 85mm")) is False
        assert C.matches_all(self.NORMAL, "x", self._dims("降雨 100mm")) is True

    def test_年型不符不命中(self):
        dims = self._dims("干旱年降雨 90mm")
        assert dims["season"] == "干旱年"
        assert C.matches_all(self.NORMAL, "x", dims) is False

    def test_水位档位要精确对上(self):
        assert C.matches_all(self.NORMAL, "x", self._dims("雨前库水位 338.5m")) is True
        # 340m 是干旱年的档位，不是"超过 338.5 也算"——否则两个互斥工况同时命中
        assert C.matches_all(self.NORMAL, "x", self._dims("雨前库水位 340m")) is False

    def test_两个条件都满足才命中(self):
        """雨量和重现期同时给出时，两者都要落在这条实例上。"""
        assert C.matches_all(self.NORMAL, "x", self._dims("2年一遇，降雨 90mm")) is True
        assert C.matches_all(self.NORMAL, "x", self._dims("50年一遇，降雨 90mm")) is False

    def test_实例没声明的维度不参与(self):
        """只写了重现期的工况，遇到"降雨 90mm"不该被排除——它本来就没这一维。"""
        props = {"frequency": 0.05, "return_period": 20}
        dims = self._dims("降雨 90mm")
        assert C.matches_all(props, "x", dims) is False  # 一个维度都没比上 → 不猜
        dims["return_period"] = 20.0
        assert C.matches_all(props, "x", dims) is True

    def test_没有条件一律不命中(self):
        assert C.matches_all(self.NORMAL, "x", {}) is False

    def test_年型不能单独作为命中依据(self):
        """「300年一遇洪水（正常年）」只有重现期和年型，靠年型不该被链接进来。

        实测：问「正常年 24h 降雨 140mm」时它靠 `season` 过关，答案里混进
        「超标准洪水削峰方案 450m³/s」——那不是这条问题问的东西。
        """
        other = {"season": "正常年", "return_period": 300}
        dims = self._dims("正常年 24h 降雨 140mm")
        assert dims["season"] == "正常年"
        assert C.matches_all(other, "x", dims) is False
        # 重现期维度给出来时它才参与
        dims["return_period"] = 300.0
        assert C.matches_all(other, "x", dims) is True

    def test_阈值型水位仍走旧语义(self):
        """CQ-03 的「库水位超过正常蓄水位」不能被联合匹配改坏。"""
        props = {"level_m": 343.0}
        compares = {"level_m": "gt"}
        assert C.matches_all(props, "x", self._dims("库水位 343.2m"), compares=compares) is True
        assert C.matches_all(props, "x", self._dims("库水位 343.0m"), compares=compares) is False
