"""规则层：TBox 声明「多少年一遇归哪一级」，引擎按问题里的工况求值。

这一层要守住三件事：

1. **规则写在 YAML，不写在代码里** —— 加一档工况是加数据，不是改引擎。
2. **边界必须明确** —— 20 / 50 / 1000 落在哪一侧，代码和文档要给出同一个答案。
3. **没有边的工况也能归级**（CQ-22）—— 这是本体相对 GraphRAG 的实质差别，
   也是这一层唯一不可替代的能力。
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import rules as R
from app.services.graph.ontology.conditions import Condition
from app.services.graph.ontology.registry import ProfileError, load_profile

BASE = """
version: 1
profile_id: probe
domain: test
entity_types:
  - name: Node
    display_name: 节点
    uid_prefix: nd
    scope: local
    attributes:
      - {name: kind, type: string}
  - name: Grade
    display_name: 等级
    uid_prefix: gr
    scope: global
    attributes:
      - {name: level, type: string}
      - {name: kind, type: string}
relation_types:
  - {name: TRIGGERS, source: Node, target: Grade}
  - {name: PARENT_OF, source: Node, target: Node, subsumption: true}
"""


def _profile(tmp_path, rules: str):
    path = tmp_path / "probe.yaml"
    path.write_text(BASE + "\n" + rules, encoding="utf-8")
    return load_profile(path, register=False)


def _freq(years: float) -> Condition:
    return Condition(
        kind="frequency", raw=f"{years:g}年一遇", value=round(1.0 / years, 6), return_period=years
    )


class TestValueOf:
    def test_从年一遇工况取重现期(self):
        assert R.value_of(_freq(20), "return_period") == 20

    def test_取超越概率(self):
        assert R.value_of(_freq(20), "value") == pytest.approx(0.05)

    def test_数值工况没有重现期(self):
        cond = Condition(kind="value", raw="343.2米", value=343.2, unit="米")
        assert R.value_of(cond, "return_period") is None
        assert R.value_of(cond, "value") == 343.2


class TestRuleMatches:
    def _rule(self, tmp_path, when: str):
        reg = _profile(
            tmp_path,
            f"""
rules:
  - id: r
    type: classify
    key: return_period
    when: {when}
    emit: {{relation: TRIGGERS, target_type: Grade, target_match: {{level: A}}}}
""",
        )
        return reg.rule("r")

    def test_左开右闭(self, tmp_path):
        rule = self._rule(tmp_path, "{gt: 20, lte: 50}")
        assert not R.rule_matches(rule, 20), "下界不含"
        assert R.rule_matches(rule, 20.1)
        assert R.rule_matches(rule, 50), "上界含"
        assert not R.rule_matches(rule, 50.1)

    def test_多个条件是_and(self, tmp_path):
        rule = self._rule(tmp_path, "{gte: 10, lt: 100}")
        assert R.rule_matches(rule, 10)
        assert not R.rule_matches(rule, 100)
        assert not R.rule_matches(rule, 5)

    def test_取不到数值一律不命中(self, tmp_path):
        rule = self._rule(tmp_path, "{lte: 50}")
        assert not R.rule_matches(rule, None), "None 不能当成 0 去比，否则什么都能命中"


class TestMatchingRules:
    def test_按重现期命中对应档位(self, tmp_path):
        reg = _profile(
            tmp_path,
            """
rules:
  - id: g4
    type: classify
    key: return_period
    when: {lte: 20}
    emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: D}}
  - id: g3
    type: classify
    key: return_period
    when: {gt: 20, lte: 50}
    emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: C}}
  - id: g2
    type: classify
    key: return_period
    when: {gt: 50, lte: 1000}
    emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: B}}
  - id: g1
    type: classify
    key: return_period
    when: {gt: 1000}
    emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: A}}
""",
        )
        cases = {20: "g4", 21: "g3", 50: "g3", 51: "g2", 300: "g2", 1000: "g2", 1001: "g1"}
        for years, want in cases.items():
            hits = R.matching_rules(reg, [_freq(years)])
            assert [h.rule.id for h in hits] == [want], f"{years} 年一遇应该落到 {want}"

    def test_同一个数值不会重复命中同一条规则(self, tmp_path):
        reg = _profile(
            tmp_path,
            """
rules:
  - id: r
    type: classify
    key: return_period
    when: {lte: 50}
    emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: D}}
""",
        )
        # 一个问题里可能抽出两条一模一样的工况（"20年一遇"和"重现期20年"）
        conds = [_freq(20), _freq(20)]
        assert len(R.matching_rules(reg, conds)) == 1

    def test_没有规则时返回空(self, tmp_path):
        reg = _profile(tmp_path, "")
        assert R.matching_rules(reg, [_freq(20)]) == []


class TestTargetMatch:
    def _rule(self, tmp_path):
        reg = _profile(
            tmp_path,
            """
rules:
  - id: r
    type: classify
    key: return_period
    when: {lte: 50}
    emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: Ⅱ, kind: 响应}}
""",
        )
        return reg.rule("r")

    def test_所有条件都要对上(self, tmp_path):
        rule = self._rule(tmp_path)
        assert R.target_matches(rule, {"level": "Ⅱ", "kind": "响应"})
        assert not R.target_matches(rule, {"level": "Ⅱ"})
        assert not R.target_matches(rule, {"level": "Ⅲ", "kind": "响应"})

    def test_数字和字符串按同一种写法比(self, tmp_path):
        rule = self._rule(tmp_path)
        assert R.target_matches(rule, {"level": "Ⅱ", "kind": "响应", "extra": 1})


class TestParse:
    def _rule(self, body: str) -> str:
        return "rules:\n" + body

    def test_非法算子直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="算子"):
            _profile(
                tmp_path,
                self._rule(
                    "  - {id: r, type: classify, key: return_period, when: {between: 5},"
                    " emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: A}}}\n"
                ),
            )

    def test_缺_when_直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="when"):
            _profile(
                tmp_path,
                self._rule(
                    "  - {id: r, type: classify, key: return_period,"
                    " emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: A}}}\n"
                ),
            )

    def test_目标类型必须是关系的宾语(self, tmp_path):
        with pytest.raises(ProfileError, match="不是关系"):
            _profile(
                tmp_path,
                self._rule(
                    "  - {id: r, type: classify, key: return_period, when: {lte: 5},"
                    " emit: {relation: TRIGGERS, target_type: Node, target_match: {kind: A}}}\n"
                ),
            )

    def test_挑选实例的属性必须已声明(self, tmp_path):
        with pytest.raises(ProfileError, match="target_match 引用了未定义属性"):
            _profile(
                tmp_path,
                self._rule(
                    "  - {id: r, type: classify, key: return_period, when: {lte: 5},"
                    " emit: {relation: TRIGGERS, target_type: Grade, target_match: {ghost: A}}}\n"
                ),
            )

    def test_id_必须唯一(self, tmp_path):
        with pytest.raises(ProfileError, match="重复"):
            _profile(
                tmp_path,
                self._rule(
                    "  - {id: r, type: classify, key: return_period, when: {lte: 5},"
                    " emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: A}}}\n"
                    "  - {id: r, type: classify, key: return_period, when: {gt: 5},"
                    " emit: {relation: TRIGGERS, target_type: Grade, target_match: {level: B}}}\n"
                ),
            )


class TestSubsumption:
    def test_标记被解析出来(self, tmp_path):
        reg = _profile(tmp_path, "")
        assert reg.subsumption_relations() == ("PARENT_OF",)

    def test_没标的关系统统不算包含关系(self, tmp_path):
        reg = _profile(tmp_path, "")
        assert "TRIGGERS" not in reg.subsumption_relations()


class TestReservoirProfile:
    """真实本体的规则必须真的存在——否则 CQ-01/CQ-22 还是空的。"""

    @pytest.fixture(autouse=True)
    def _reg(self):
        from app.services.graph.ontology import reset_cache

        reset_cache()
        self.reg = load_profile("reservoir_flood", register=False)

    def test_四档规则都在且有_cq(self):
        assert len(self.reg.rules) == 4
        for rule in self.reg.rules.values():
            assert rule.cq, f"{rule.id} 没写 cq"

    def test_各级重现期都归到正确的等级(self):
        cases = {10: "Ⅳ", 20: "Ⅳ", 21: "Ⅲ", 50: "Ⅲ", 51: "Ⅱ", 100: "Ⅱ", 300: "Ⅱ", 1000: "Ⅱ", 1500: "Ⅰ"}
        for years, level in cases.items():
            hits = R.matching_rules(self.reg, [_freq(years)])
            assert len(hits) == 1, f"{years} 年一遇命中了 {[h.rule.id for h in hits]}"
            assert hits[0].rule.target_match == {"level": level}, f"{years} 年一遇应为 {level} 级"

    def test_INSTANCE_OF_被声明成包含关系(self):
        assert "INSTANCE_OF" in self.reg.subsumption_relations()
