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
    _clean_latex,
    _extract_toc_map,
    _parse_numbered_heading,
    _parse_chapter_heading,
    _guess_heading_level,
    _parse_outline_number,
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

    def test_decimal_spaces_merged(self):
        """单元 B：小数空格合并（2 . 5 → 2.5、0 . 9 5 → 0.95）。"""
        assert _norm_mineru_text("H = 0 . 5 m") == "H = 0.5 m"
        assert _norm_mineru_text("0 . 9 5 × 1 . 3 3") == "0.95 × 1.33"
        assert _norm_mineru_text("t_s = 2 . 4 q") == "t_s = 2.4 q"

    def test_decimal_spaces_not_overmerge(self):
        """单元 B 防误伤：正常小数/列表项/编号/章节号不动。"""
        # 正常小数（点两侧无空格）不动
        assert _norm_mineru_text("深 h 为 3.6m") == "深 h 为 3.6m"
        # 列表项「1. 2」（点前无空格）不动
        assert _norm_mineru_text("1. 总压力") == "1. 总压力"
        # 编号+章节号（点前无空格，如 7.2.4）不动
        assert _norm_mineru_text("22 7.2.4 工控网") == "22 7.2.4 工控网"
        # 中文句号不是小数点（点两侧无空格）不动
        assert _norm_mineru_text("第 3.4 节") == "第 3.4 节"


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
        """image → FIGURE，无图注时 IR 层生成可检索占位「图 pXX」。"""
        elements = adapt_mineru_output(content_list, middle)
        figs = [e for e in elements if e.type == ElementType.FIGURE]
        assert figs and figs[0].text == "图 p2"  # fixture page_idx=1 → 页 2

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

    def test_figure_keeps_block_type(self, content_list, middle):
        """单元 A：figure 保留 block_type="figure"，不再降级 paragraph。"""
        elements = adapt_mineru_output(content_list, middle)
        blocks = mineru_to_blocks(elements)
        figs = [b for b in blocks if b.block_type == "figure"]
        assert figs, "图元素应保留 figure 类型"

    def test_figure_without_caption_gets_placeholder(self, content_list, middle):
        """单元 A：无图注的图片生成「图 pXX」占位，不再产生空文本块。"""
        elements = adapt_mineru_output(content_list, middle)
        blocks = mineru_to_blocks(elements)
        figs = [b for b in blocks if b.block_type == "figure"]
        assert figs and all(b.text.strip() for b in figs), "图片块文本不得为空"

    def test_figure_placeholder_survives_into_chunks(self, content_list, middle):
        """单元 A 补漏：figure 占位必须穿过 parent-child 切片进 chunk（此前空 figure 被跳过）。"""
        from app.services.chunking.parent_child import build_parent_child

        elements = adapt_mineru_output(content_list, middle)
        pc = build_parent_child(elements, blocks=[])
        blob = " ".join(c.content for c in pc)
        assert "图 p2" in blob, "figure 占位文本应出现在切片中"


class TestCleanLatex:
    def test_tag_to_paren(self):
        """\\tag{3.15} → (3.15)。"""
        assert _clean_latex(r"Q = A_1 v_1 = A_2 v_2\tag{3.15}") == "Q = A_1 v_1 = A_2 v_2 (3.15)"

    def test_begin_end_array_removed(self):
        """\\begin{array}{ll} 环境被清理，无 \\\\ 和 & 残留。"""
        out = _clean_latex(r"\begin{array}{l l} f x = F x / m \\ f y = F y / m \end{array}")
        assert "\\array" not in out
        assert "\\begin" not in out
        assert "\\end" not in out
        assert "\\\\" not in out
        assert "&" not in out
        assert "f x = F x / m" in out

    def test_subscript_superscript_preserved(self):
        """下标/上标保留且紧凑化：A _ { 1 } → A_1。"""
        assert _clean_latex(r"F _ { \mathrm { p } } = \rho g h") == "F_p = ρ g h"
        assert _clean_latex(r"L ^ { \alpha }") == "L^α"

    def test_frac_tag_combined(self):
        """\\frac + \\tag 组合正确。"""
        out = _clean_latex(r"$$\rho = \frac{m}{V}\tag{1.3}$$")
        assert "(1.3)" in out
        assert "ρ" in out

    def test_escaped_vertical_bar_removed(self):
        """转义竖线 \\| 是噪声（绝对值/分数线 OCR 误识别），清洗后不残留反斜杠。"""
        out = _clean_latex(r"p^′ = p_\|")
        assert "\\" not in out
        assert "\\|" not in out

    def test_escaped_punctuation_restored(self):
        """转义标点 \\~ \\% \\_ \\^ 还原成本字符；反斜杠+空格清掉（回归「公式乱码」）。"""
        assert _clean_latex(r"0.5\~1.0m") == "0.5~1.0m"
        assert _clean_latex(r"4 \% 温度") == "4 % 温度"
        assert _clean_latex(r"p_2 \_- 2") == "p_2_- 2"
        assert _clean_latex(r"x\^2") == "x^2"
        assert _clean_latex(r"a\ b") == "a b"

    def test_varrho_greek(self):
        """希腊字母变体 \\varrho → ρ。"""
        assert _clean_latex(r"\varrho g h") == "ρ g h"


class TestExtractToc:
    def test_two_dot_ellipsis_extracted(self):
        """两点的省略号行（……28）也提取（此前 {3,} 漏掉）。"""
        content = [{
            "type": "text",
            "text": (
                "1 绪论1\n"
                "1.1 水力学的任务与研究对象…………………1\n"
                "1.2 水力学发展简史……2\n"
                "2 水静力学……19\n"
                "2.3 重力作用下静水压强的基本公式……28\n"
                "2.4 重力和惯性力同时作用下的液体平衡……35\n"
                "3.4 恒定总流的能量方程……74\n"
            ),
        }]
        toc = _extract_toc_map(content)
        assert "2.3" in toc
        assert toc["2.3"] == ("重力作用下静水压强的基本公式", 2)
        assert "3.4" in toc
        assert toc["3.4"] == ("恒定总流的能量方程", 2)

    def test_level3_excluded(self):
        """三级编号不进 TOC。"""
        content = [{
            "type": "text",
            "text": (
                "1 绪论1\n"
                "1.1 任务……1\n"
                "1.2 历史……2\n"
                "1.3 性质……5\n"
                "1.1.1 子节……2\n"
                "1.4 概念……13\n"
            ),
        }]
        toc = _extract_toc_map(content)
        assert "1.1" in toc
        assert "1.1.1" not in toc


class TestParseNumberedHeading:
    def test_list_item_rejected(self):
        """列表项「1. 总压力的大小」不是章标题。"""
        assert _parse_numbered_heading("1. 总压力的大小") is None

    def test_chapter_heading_accepted(self):
        """「3.1 描述液体运动的方法」是二级标题。"""
        assert _parse_numbered_heading("3.1 描述液体运动的方法") == ("3.1", 2)

    def test_date_rejected(self):
        """「1979年2月」不是标题。"""
        assert _parse_numbered_heading("1979年2月") is None


class TestOutlineNumberGeneric:
    """单元 A：大纲编号识别通用化——支持多种编号体系，新老 MinerU 共用。"""

    def test_arabic_level1(self):
        assert _parse_outline_number("1 绪论") == ("1", 1, "绪论")
        assert _parse_outline_number("12 防洪减灾") == ("12", 1, "防洪减灾")

    def test_arabic_dotted_level2_3(self):
        assert _parse_outline_number("1.1 水力学的任务") == ("1.1", 2, "水力学的任务")
        assert _parse_outline_number("1.1.1 子节") == ("1.1.1", 3, "子节")

    def test_arabic_list_item_rejected(self):
        assert _parse_outline_number("1. 总压力的大小") is None

    def test_chapter_arabic(self):
        assert _parse_outline_number("第1章 绪论") == ("1", 1, "绪论")
        assert _parse_outline_number("第 5章 防洪减灾") == ("5", 1, "防洪减灾")

    def test_chapter_cn_digit(self):
        assert _parse_outline_number("第一章 绪论") == ("1", 1, "绪论")
        assert _parse_outline_number("第十二章 渗流") == ("12", 1, "渗流")

    def test_section_cn_digit(self):
        assert _parse_outline_number("第一节 概述") == ("C.1", 2, "概述")
        assert _parse_outline_number("第三节 计算方法") == ("C.3", 2, "计算方法")

    def test_section_paren_cn_digit(self):
        assert _parse_outline_number("（一）概述") == ("C.1", 2, "概述")
        assert _parse_outline_number("(三) 计算方法") == ("C.3", 2, "计算方法")

    def test_cn_digit_dun_level1(self):
        assert _parse_outline_number("一、绪论") == ("1", 1, "绪论")
        assert _parse_outline_number("二 水静力学") == ("2", 1, "水静力学")

    def test_date_rejected(self):
        assert _parse_outline_number("1979年2月") is None

    def test_formula_rejected(self):
        assert _parse_outline_number("1 φ=ρg") is None


class TestGuessHeadingGeneric:
    """单元 A：_guess_heading_level 通用化。"""

    def test_chinese_chapter(self):
        assert _guess_heading_level("第一章 绪论") == 1

    def test_chinese_dun(self):
        assert _guess_heading_level("二、水静力学") == 1

    def test_arabic_dotted(self):
        assert _guess_heading_level("7.4 地图版面布局") == 2

    def test_arabic_level1(self):
        assert _guess_heading_level("6 避洪转移分析") == 1

    def test_section_cn(self):
        assert _guess_heading_level("第一节 概述") == 2


class TestExtractTocGeneric:
    """单元 A：目录提取支持多种大纲体系，统一成阿拉伯点分。"""

    def test_cn_chapter_and_dun(self):
        content = [{
            "type": "text",
            "text": (
                "第一章 绪论……1\n"
                "第一节 任务……1\n"
                "第二节 发展史……2\n"
                "第二章 水静力学……19\n"
                "第一节 静水压强……19\n"
                "第二节 微分方程……23\n"
            ),
        }]
        toc = _extract_toc_map(content)
        # 中文章号转阿拉伯；相对节号由当前章补全
        assert toc["1"] == ("绪论", 1)
        assert toc["1.1"] == ("任务", 2)
        assert toc["1.2"] == ("发展史", 2)
        assert toc["2"] == ("水静力学", 1)
        assert toc["2.1"] == ("静水压强", 2)
        assert toc["2.2"] == ("微分方程", 2)

    def test_paren_cn_digit(self):
        content = [{
            "type": "text",
            "text": (
                "一、绪论1\n"
                "（一）任务……1\n"
                "（二）发展史……2\n"
                "二、水静力学……19\n"
                "（一）静水压强……19\n"
            ),
        }]
        toc = _extract_toc_map(content)
        assert toc["1"] == ("绪论", 1)
        assert toc["1.1"] == ("任务", 2)
        assert toc["1.2"] == ("发展史", 2)
        assert toc["2"] == ("水静力学", 1)
        assert toc["2.1"] == ("静水压强", 2)

    def test_mixed_arabic_still_works(self):
        """原有阿拉伯点分体系不受通用化影响。"""
        content = [{
            "type": "text",
            "text": (
                "1 绪论1\n"
                "1.1 任务……1\n"
                "1.2 历史……2\n"
                "2 水静力学……19\n"
                "2.1 静水压强……19\n"
            ),
        }]
        toc = _extract_toc_map(content)
        assert toc["1"] == ("绪论", 1)
        assert toc["1.1"] == ("任务", 2)
        assert toc["2"] == ("水静力学", 1)
        assert toc["2.1"] == ("静水压强", 2)

    def test_toc_split_across_blocks_merged(self):
        """单元 B：目录跨多个文本块（hybrid 拆页）必须合并，不能只取一块。

        回归背景：hybrid 后端把目录拆成 p10（第1~4章）+ p11（4.5~第9章）两块，
        旧实现用 max() 只挑数字最多的一块 → 前 4 章整段丢失，二级标题全部降级。
        """
        content = [
            {
                "type": "text",
                "text": (
                    "第1章 绪论……1\n"
                    "1.1 任务……1\n"
                    "1.2 历史……2\n"
                    "第2章 水静力学……19\n"
                    "2.1 静水压强……19\n"
                ),
            },
            {
                "type": "text",
                "text": (
                    "第3章 液体运动……39\n"
                    "3.1 描述方法……39\n"
                    "3.2 基本概念……41\n"
                    "3.3 连续性方程……45\n"
                    "3.4 能量方程……50\n"
                ),
            },
        ]
        toc = _extract_toc_map(content)
        # 两块都要合并进来，前 4 章不能丢
        assert "1" in toc and toc["1"] == ("绪论", 1)
        assert "1.1" in toc
        assert "2" in toc and toc["2"] == ("水静力学", 1)
        assert "2.1" in toc
        assert "3" in toc and toc["3"] == ("液体运动", 1)
        assert "3.1" in toc
        assert "3.4" in toc
