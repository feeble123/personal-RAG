"""P1-2 单元C：MinerU → DocumentElement IR adapter 测试。

用 fixture JSON（evaluation/fixtures/mineru_fake_*.json），不真跑 MinerU。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.parser.ir import ElementType
from app.services.parser.ir_validation import validate_elements
from app.services.parser.mineru import (
    adapt_mineru_output,
    mineru_to_blocks,
    _norm_mineru_text,
    _parse_table_html,
)

FIXTURES = Path(__file__).resolve().parents[2] / "evaluation" / "fixtures"


def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def content_list():
    return _load("mineru_fake_content_list.json")


@pytest.fixture
def middle():
    return _load("mineru_fake_middle.json")


class TestNormText:
    def test_cjk_gap_removed(self):
        """中文间空格去除。"""
        assert _norm_mineru_text("表 1 参数") == "表1 参数"
        assert _norm_mineru_text("明渠 均匀流") == "明渠均匀流"

    def test_date_hyphen(self):
        """日期连字符归一。"""
        assert _norm_mineru_text("2023 - 11 - 14") == "2023-11-14"


class TestTableParse:
    def test_parse_table_html_rows(self):
        """HTML 表格 → rows + header_path。"""
        html = "<table><tr><td>参数</td><td>取值</td></tr><tr><td>糙率</td><td>0.025</td></tr></table>"
        t = _parse_table_html(html)
        assert t is not None
        assert t["header_path"] == ["参数", "取值"]
        assert t["rows"][1] == ["糙率", "0.025"]

    def test_parse_table_bad_html_fallback(self):
        """畸形 HTML → 降级 None（不抛）。"""
        assert _parse_table_html("<table>broken") is None
        assert _parse_table_html("") is None


class TestAdapt:
    def test_excludes_header_footer(self, content_list, middle):
        """page_number 和 footer 不进 elements；header 保留（MinerU 把章节标题也标为 header）。"""
        elements = adapt_mineru_output(content_list, middle)
        texts = [e.text for e in elements]
        # footer 排除
        assert "第 1 页 共 2 页" not in texts
        # page_number 排除
        # header 保留（如 '1 绪论' 被 MinerU 标为 header）

    def test_heading_level_mapped(self, content_list, middle):
        """编号标题（如 1.1 适用范围）→ heading_level=2，无 inferred_heading flag。"""
        elements = adapt_mineru_output(content_list, middle)
        heading = [e for e in elements if e.type == ElementType.HEADING and "1.1" in e.text]
        assert heading
        assert heading[0].heading_level == 2
        assert "inferred_heading" not in heading[0].flags
        assert "layout_model" in heading[0].flags

    def test_table_structure(self, content_list, middle):
        """table_body HTML → table 字段 + 过 validator。"""
        elements = adapt_mineru_output(content_list, middle)
        tables = [e for e in elements if e.type == ElementType.TABLE]
        assert tables
        assert tables[0].table is not None
        assert tables[0].table["header_path"] == ["参数", "取值"]
        # 整体过 validator（表格行列一致）
        assert validate_elements(elements) == []

    def test_reading_order(self, content_list, middle):
        """elements 顺序 = content_list 顺序（reading_order == index）。"""
        elements = adapt_mineru_output(content_list, middle)
        for i, e in enumerate(elements):
            assert e.reading_order == i

    def test_bbox_normalized(self, content_list, middle):
        """bbox 4 元 float。"""
        elements = adapt_mineru_output(content_list, middle)
        titled = [e for e in elements if e.text == "水利工程规范测试"]
        assert titled and titled[0].bbox == (120.0, 90.0, 880.0, 130.0)

    def test_figure_mapped(self, content_list, middle):
        """image → FIGURE。"""
        elements = adapt_mineru_output(content_list, middle)
        figs = [e for e in elements if e.type == ElementType.FIGURE]
        assert figs and figs[0].text == ""

    def test_elements_validate(self, content_list, middle):
        """整体过 IR validator。"""
        elements = adapt_mineru_output(content_list, middle)
        errors = validate_elements(elements)
        assert errors == []

    def test_source_ref_has_version(self, content_list, middle):
        """source_ref 带 parser 版本。"""
        elements = adapt_mineru_output(content_list, middle)
        assert elements[0].source_ref["parser"] == "mineru"
        assert elements[0].source_ref["parser_version"] == "3.4.4"


class TestBlocksCompat:
    def test_mineru_to_blocks(self, content_list, middle):
        """IR elements → ParsedBlock 类型映射正确。"""
        elements = adapt_mineru_output(content_list, middle)
        blocks = mineru_to_blocks(elements)
        types = {b.block_type for b in blocks}
        assert "heading" in types
        assert "table" in types
        assert "paragraph" in types
        # 标题块是块，非表格
        headings = [b for b in blocks if b.block_type == "heading"]
        assert any("1.1" in b.text for b in headings)
