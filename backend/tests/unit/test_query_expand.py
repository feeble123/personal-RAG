"""单元 N：口语→术语 查询扩展回归测试。

锁定：命中口语词追加规范术语（只加词不删词）+ 不误伤（歧义词上下文守卫 /
非口语词原样返回 / 空输入回退）。
"""
from __future__ import annotations

from app.services.query_expand import expand_query


class TestPositiveExpansion:
    """口语/宽泛表达 → 追加规范术语。"""

    def test_wenbuwen_plus_stability(self):
        out = expand_query("山坡上的土石要往下滑，怎么看它稳不稳？")
        assert "稳定性" in out
        assert "稳不稳" in out  # 只加词不删词

    def test_jiannaxie_plus_zucheng(self):
        out = expand_query("农村供水的工程要建哪些东西？")
        assert "系统组成" in out

    def test_anchangguan_plus_jisuan(self):
        out = expand_query("水在管子里流，什么时候可以直接按长管算？")
        assert "长管计算依据" in out

    def test_pingwenhuanluan_plus_liutai(self):
        out = expand_query("怎么判断水流是平稳的还是乱的？")
        assert "层流" in out and "紊流" in out


class TestGuardedExpansion:
    """歧义词「垮了」→ 溃坝：只在句含坝/堤/堰/库时追加。"""

    def test_dam_kuata_plus_kuiba(self):
        out = expand_query("大坝万一垮了怎么办？")
        assert "溃坝" in out

    def test_bridge_kuata_no_kuiba(self):
        # 「桥垮了」不该补「溃坝」
        out = expand_query("桥垮了怎么办？")
        assert "溃坝" not in out


class TestNoRegression:
    """非口语问句 / 边界输入原样返回，不误伤。"""

    def test_plain_question_unchanged(self):
        assert expand_query("什么是雷诺数？") == "什么是雷诺数？"

    def test_empty_unchanged(self):
        assert expand_query("") == ""
        assert expand_query("   ") == "   "

    def test_none_safe(self):
        # 守卫：非字符串输入回退（不抛异常）
        assert expand_query(None) is None  # type: ignore[arg-type]

    def test_no_partial_match(self):
        # 「稳定性」本身已是术语，不重复追加
        assert expand_query("怎么看滑坡稳定性？") == "怎么看滑坡稳定性？"
