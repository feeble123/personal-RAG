"""条款断号自检单元测试：检测 OCR 偶发漏行的条款缺失。"""
from __future__ import annotations

from app.services.parser.base import ParsedBlock
from app.services.parser.clause_gap import _contains_clause, check_clause_gaps


def _blk(text: str, section: str | None = None, page: int | None = None) -> ParsedBlock:
    return ParsedBlock(text=text, section=section, page=page)


class TestContainsClause:
    def test_boundary_no_partial(self):
        # 8.2.3 不应匹配 8.2.30 / 18.2.3
        assert _contains_clause("见8.2.3条", "8.2.3") is True
        assert _contains_clause("见8.2.30条", "8.2.3") is False
        assert _contains_clause("见18.2.3条", "8.2.3") is False

    def test_space_after_number(self):
        assert _contains_clause("8.2.3 进行比选", "8.2.3") is True


class TestCheckClauseGaps:
    def test_detects_missing_in_middle(self):
        # 8.2.3 缺失（OCR 漏行场景，8.2.1/8.2.2/8.2.4 存在）
        blocks = [
            _blk("8.2.1在防治阶段，应首先进行比选。", "8设计方案选择 / 8.2方案比选", 12),
            _blk("8.2.2应进行两个以上工程方案的比选。", "8设计方案选择 / 8.2方案比选", 12),
            _blk("8.2.4方案比选应从技术、经济两方面确定。", "8设计方案选择 / 8.2方案比选", 12),
        ]
        gaps = check_clause_gaps(blocks)
        assert len(gaps) == 1
        g = gaps[0]
        assert g["section"] == "8.2"
        assert g["missing"] == [3]
        assert "8.2.3" in g["missing_full"]
        assert g["pages"] == [12]

    def test_number_directly_followed_by_chinese(self):
        # 条款号后无空格也须识别（真实 OCR 常见）
        blocks = [_blk("3.2.1引用标准应为现行标准", None, 3), _blk("3.2.3引用标准应为有效版本", None, 3)]
        gaps = check_clause_gaps(blocks)
        assert len(gaps) == 1
        assert gaps[0]["missing"] == [2]

    def test_complete_no_gap(self):
        blocks = [
            _blk("8.2.1内容A", None, 1),
            _blk("8.2.2内容B", None, 1),
            _blk("8.2.3内容C", None, 1),
            _blk("8.2.4内容D", None, 1),
        ]
        assert check_clause_gaps(blocks) == []

    def test_single_clause_no_gap(self):
        assert check_clause_gaps([_blk("8.2.1只有一条", None, 1)]) == []

    def test_date_not_counted_as_clause(self):
        blocks = [_blk("2020.10.01发布", None, 1), _blk("8.2.1内容A", None, 1), _blk("8.2.3内容C", None, 1)]
        gaps = check_clause_gaps(blocks)
        assert len(gaps) == 1
        assert gaps[0]["missing"] == [2]

    def test_multiple_sections(self):
        blocks = [
            _blk("6.1.1内容", "6", 5),
            _blk("6.1.3内容", "6", 5),
            _blk("9.2.3内容", "9", 8),
            _blk("9.2.7内容", "9", 8),
        ]
        gaps = check_clause_gaps(blocks)
        secs = {g["section"]: g["missing"] for g in gaps}
        assert secs == {"6.1": [2], "9.2": [4, 5, 6]}

    def test_cross_reference_in_prose(self):
        # 正文引用（按8.2.3的规定）也算该条存在 → 相邻条款齐全时不触发缺失
        blocks = [
            _blk("8.2.1内容", None, 1),
            _blk("8.2.2内容", None, 1),
            _blk("按8.2.3的规定执行", None, 1),
            _blk("8.2.4内容", None, 1),
        ]
        assert check_clause_gaps(blocks) == []
