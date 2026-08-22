"""切片内容完整性自检单元测试：只增不减（原文件每行必须保留在切片中）。"""
from __future__ import annotations

from app.services.chunker import Chunk, _hash
from app.services.parser.base import ParsedBlock
from app.services.parser.completeness import check_content_completeness


def _chunk(content: str, section: str | None = None, page: int = 1) -> Chunk:
    return Chunk(content=content, section=section, page=page, content_hash=_hash(content))


class TestContentCompleteness:
    def test_all_content_preserved(self):
        """标题 + 正文全部保留 → complete。"""
        blocks = [
            ParsedBlock(text="1 总则", page=4, block_type="heading"),
            ParsedBlock(text="本规范适用于给水排水工程。", page=4, block_type="paragraph"),
        ]
        chunks = [_chunk("## 1 总则\n本规范适用于给水排水工程。")]
        res = check_content_completeness(blocks, chunks)
        assert res["complete"] is True
        assert res["missing_lines"] == 0

    def test_missing_line_detected(self):
        """某行不在任何切片 → 报缺失（含页码与样例）。"""
        blocks = [
            ParsedBlock(text="条文说明内容", page=31, block_type="paragraph"),
        ]
        chunks = [_chunk("只有这段正文")]
        res = check_content_completeness(blocks, chunks)
        assert res["complete"] is False
        assert res["missing_lines"] == 1
        assert res["missing_pages"] == [31]
        assert "条文说明内容" in res["sample"][0]["text"]

    def test_short_lines_exempt(self):
        """短行（页码/标记，<4 字）豁免，不误报。"""
        blocks = [ParsedBlock(text="12", page=5, block_type="paragraph")]
        res = check_content_completeness(blocks, [_chunk("正文")])
        assert res["complete"] is True

    def test_long_line_skipped(self):
        """超长行（被二次切分跨块）豁免，不误报。"""
        blocks = [ParsedBlock(text="甲" * 150, page=5, block_type="paragraph")]
        chunks = [_chunk("甲" * 100), _chunk("甲" * 50)]
        res = check_content_completeness(blocks, chunks)
        assert res["complete"] is True
        assert res["skipped_long_lines"] == 1

    def test_heading_covered_via_section_prefix(self):
        """标题文本经章节前缀保留在切片里 → 不算缺失。"""
        blocks = [
            ParsedBlock(text="3.2 永久作用标准值", page=12, block_type="heading"),
            ParsedBlock(text="内容", page=12, block_type="paragraph"),
        ]
        res = check_content_completeness(blocks, [_chunk("## 3.2 永久作用标准值\n内容", page=12)])
        assert res["complete"] is True

    def test_page_coverage_ok(self):
        """有正文内容的页都存在 page 相同的 chunk → 页级覆盖完整。"""
        blocks = [ParsedBlock(text="第27页内容", page=27, block_type="paragraph")]
        res = check_content_completeness(blocks, [_chunk("第27页内容", page=27)])
        assert res["page_coverage"]["complete"] is True
        assert res["page_coverage"]["uncovered_pages"] == []

    def test_page_coverage_detects_merge(self):
        """内容被合并进相邻页 chunk（页号错位）→ 报 uncovered。"""
        blocks = [ParsedBlock(text="第27页内容", page=27, block_type="paragraph")]
        res = check_content_completeness(blocks, [_chunk("第27页内容", page=28)])
        assert res["page_coverage"]["complete"] is False
        assert res["page_coverage"]["uncovered_pages"] == [27]

    def test_heading_only_page_exempt(self):
        """纯标题页（无正文内容块）不报 uncovered（标题只作前缀）。"""
        blocks = [ParsedBlock(text="5 基本构造要求", page=26, block_type="heading")]
        res = check_content_completeness(blocks, [_chunk("## 5 基本构造要求\n后续内容", page=27)])
        assert res["page_coverage"]["complete"] is True
