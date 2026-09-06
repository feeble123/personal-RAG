"""单元E：引用忠实校验的确定性判定逻辑单测。

覆盖 verify_citations 的两层确定性校验：
- 编号合规（bad_numbers）：[n] 越界/编造编号 → ok=False
- 引用相关（unsupported_numbers）：编号合法但标注句 vs 资料原文词面重叠过低 → 挂错引用
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services import verify


def _cite(snippet: str) -> SimpleNamespace:
    """构造仅含 snippet 字段的引用对象（verify_citations 只读 snippet）。"""
    return SimpleNamespace(snippet=snippet)


class TestNumberCompliance:
    async def test_out_of_range_citation(self):
        """引用编号超出资料条数 → ok=False + bad_numbers 标出。"""
        cites = [_cite("明渠均匀流形成条件"), _cite("谢才公式")]
        verdict = await verify.verify_citations("答案是如此[5]", cites)
        assert verdict.ok is False
        assert verdict.bad_numbers == [5]
        assert verdict.unsupported_numbers == []

    async def test_in_range_ok(self):
        """编号在范围内 → ok=True，bad_numbers 为空。"""
        cites = [_cite("明渠均匀流形成条件"), _cite("谢才公式")]
        verdict = await verify.verify_citations("答案是如此[1]", cites)
        assert verdict.ok is True
        assert verdict.bad_numbers == []

    async def test_empty_answer(self):
        """空回答 → 默认全过（bad_numbers 保持 None 默认，不做越界判定）。"""
        verdict = await verify.verify_citations("", [_cite("内容")])
        assert verdict.ok is True
        assert verdict.bad_numbers is None
        assert verdict.unsupported_numbers is None


class TestRelevanceCheck:
    async def test_supported_citation_not_flagged(self):
        """标注句与资料原文词面重叠高 → 不判挂错。"""
        cites = [_cite("明渠均匀流的形成条件包括长直棱柱体渠道、正坡、糙率不变")]
        answer = "明渠均匀流需要长直棱柱体渠道和正坡[1]"
        verdict = await verify.verify_citations(answer, cites)
        assert verdict.ok is True
        assert verdict.unsupported_numbers == []

    async def test_unrelated_citation_flagged(self):
        """标注句与资料原文毫无词面重叠 → 判挂错引用。"""
        cites = [_cite("明渠均匀流的流速公式为谢才公式")]
        answer = "大坝的泄洪能力取决于溢洪道宽度和闸门尺寸[1]"
        verdict = await verify.verify_citations(answer, cites)
        assert verdict.ok is True  # 编号合法，ok 仍为 True（合规层）
        assert verdict.unsupported_numbers == [1]  # 相关性层标出

    async def test_multiple_citations_mixed(self):
        """多引用中，只标出真正挂错的那个编号，其余不误报。"""
        cites = [
            _cite("明渠均匀流的形成条件包括长直棱柱体渠道、正坡"),
            _cite("静水压力随深度增加而线性增大，任一点的静水压强等于该点以上单位面积水柱重量"),
            _cite("谢才公式用于计算明渠均匀流流速"),
        ]
        answer = "明渠需要长直棱柱体渠道和正坡[1]；其次要理解静水压力分布随深度变化[2]；流速可用谢才公式算[3]"
        verdict = await verify.verify_citations(answer, cites)
        assert verdict.ok is True
        assert verdict.unsupported_numbers == [], f"不该误报: {verdict.unsupported_numbers}"
