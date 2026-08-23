"""P1 工作包B：回答质量评测的判定逻辑单测。

覆盖 _fact_check 的容错匹配：
- hint 核心词与回答表述差异（「长直」vs「长而直」）→ 仍算命中
- 回答缺关键概念 → 判 False
- hint 为 None → 不判定
"""
from __future__ import annotations

from evaluation.answer_eval import _fact_check


class TestFactCheck:
    def test_exact_match(self):
        assert _fact_check("明渠均匀流形成条件包括长直棱柱体渠道、正坡、糙率不变、流量恒定",
                           "明渠均匀流形成条件：长直棱柱体渠道、正坡、糙率不变、流量恒定") is True

    def test_paraphrase_match(self):
        """表述差异（长而直 vs 长直；糙率必须沿程不变 vs 糙率不变）仍应命中。"""
        ans = ("渠道必须是长而直的棱柱体渠道，断面形状和尺寸沿程不变，"
               "糙率必须沿程不变，流量恒定，渠底必须为正坡")
        hint = "明渠均匀流形成条件：长直棱柱体渠道、正坡、糙率不变、流量恒定"
        assert _fact_check(ans, hint) is True

    def test_missing_concept_returns_false(self):
        """回答缺关键概念（无正坡）→ 判 False。"""
        ans = ("渠道必须是长而直的棱柱体渠道，断面形状和尺寸沿程不变，"
               "糙率必须沿程不变，流量恒定")  # 缺正坡
        hint = "明渠均匀流形成条件：长直棱柱体渠道、正坡、糙率不变、流量恒定"
        assert _fact_check(ans, hint) is False

    def test_no_hint_returns_none(self):
        assert _fact_check("任意回答", None) is None

    def test_general_answer_correct(self):
        ans = "径流是指大气降水降到地面后，部分沿地表和地下流动汇入河网"
        hint = "径流：降落到地面的水在重力作用下沿地表和地下流动"
        assert _fact_check(ans, hint) is True

    def test_short_hint(self):
        ans = "设计洪水系由设计暴雨推求"
        hint = "设计洪水由暴雨推求（由暴雨推求设计洪水）"
        assert _fact_check(ans, hint) is True
