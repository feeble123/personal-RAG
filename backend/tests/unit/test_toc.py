"""目录（TOC）解析单元测试：条目解析 / 目录页判定 / 页码偏移对齐。"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.parser.toc import (
    TocEntry,
    _parse_page_entries,
    align_pages,
    collect_toc_entries,
    compute_offset,
    continues_toc,
    is_toc_page,
    parse_toc_line,
)


def _heading(text: str, page: int):
    return SimpleNamespace(block_type="heading", text=text, page=page)


class TestParseTocLine:
    def test_dotted_leader(self):
        e = parse_toc_line("1. 总则........... 1")
        assert e is not None
        assert (e.number, e.title, e.printed_page, e.level) == ("1", "总则", 1, 1)

    def test_level2_and_paren_page(self):
        e = parse_toc_line("1.1 适用范围 ......... 3")
        assert (e.number, e.title, e.printed_page, e.level) == ("1.1", "适用范围", 3, 2)

    def test_level3(self):
        e = parse_toc_line("3.2.1 三级条款 ....... 12")
        assert (e.number, e.title, e.level) == ("3.2.1", "三级条款", 3)

    def test_chinese_paren_page(self):
        e = parse_toc_line("1 总则……（5）")
        assert (e.number, e.title, e.printed_page) == ("1", "总则", 5)

    def test_plain_trailing_page(self):
        e = parse_toc_line("2 应急保障 8")
        assert (e.number, e.title, e.printed_page) == ("2", "应急保障", 8)

    def test_no_number_title_only(self):
        e = parse_toc_line("总则 ......... 1")
        assert e is not None
        assert (e.number, e.title, e.level) == ("", "总则", 1)

    def test_enumerated_title_with_juhao(self):
        """带、的章节标题（可变作用标准值、准永久值系数）可解析——、不是坏标点。"""
        e = parse_toc_line("3.3 可变作用标准值、准永久值系数 ......... 7")
        assert e is not None
        assert (e.number, e.title, e.printed_page) == ("3.3", "可变作用标准值、准永久值系数", 7)

    def test_reject_noise(self):
        assert parse_toc_line("") is None
        assert parse_toc_line("― 2 ―") is None
        assert parse_toc_line("第 3 页") is None
        assert parse_toc_line("目 录") is None  # 目录页自身的标题
        assert parse_toc_line("1. 总则，包括适用范围。........ 1") is None  # 含句读
        assert parse_toc_line("一二三这是一行没有页码的长内容啊") is None  # 无编号无页码


class TestIsTocPage:
    def test_real_toc_page(self):
        text = "目 录\n1. 总则........... 1\n1.1 适用范围 ... 2\n2 应急保障 ... 5\n"
        assert is_toc_page(text, min_entries=3) is True

    def test_body_title_not_toc(self):
        # 「目录管理」出现在正文标题，但无有效目录条目 → 不误报
        assert is_toc_page("目录管理\n这是正文内容，没有目录条目的格式。", min_entries=3) is False
        # 目录关键词在但条目不足
        assert is_toc_page("目录设置\n第1章 概 述", min_entries=3) is False

    def test_non_monotonic_rejected(self):
        text = "目 录\n1. 总则........... 5\n2 应急保障 ... 2\n3 其他 ... 8\n"  # 5→2→8 倒序
        assert is_toc_page(text, min_entries=3) is False


class TestComputeOffset:
    def test_majority_vote(self):
        entries = [
            TocEntry(number="1", title="总则", printed_page=1, level=1),
            TocEntry(number="1.1", title="适用范围", printed_page=2, level=2),
            TocEntry(number="1.2", title="组织指挥", printed_page=2, level=2),
            TocEntry(number="2", title="应急保障", printed_page=5, level=1),
        ]
        blocks = [
            _heading("1 总则", 4),
            _heading("1.1 适用范围", 5),
            _heading("1.2 组织指挥", 5),
            _heading("2 应急保障", 8),
        ]
        assert compute_offset(entries, blocks, min_matches=2) == 3  # 4-1=3, 5-2=3, 8-5=3

    def test_precise_number_match(self):
        # 目录条目 1 不得误配正文 1.2 / 10 标题
        entries = [TocEntry(number="1", title="总则", printed_page=1, level=1)]
        blocks = [
            _heading("1.2 组织指挥", 5),
            _heading("10 其他规定", 9),
            _heading("1 总则", 4),
        ]
        assert compute_offset(entries, blocks, min_matches=2) is None  # 仅 1 条匹配 < 2

    def test_insufficient_matches(self):
        entries = [TocEntry(number="99", title="不存在", printed_page=1, level=1)]
        blocks = [_heading("1 总则", 4)]
        assert compute_offset(entries, blocks, min_matches=2) is None


class TestMessyTocParsing:
    """真实 PDF 乱格式目录（GB 50332 实测：拆行编号/独立页码/点线独立行）。"""

    def test_split_number_and_title(self):
        text = "目次\n2 \n主要符号........... 2 \n3.1 \n作用分类和作用代表值\n…………………………… 5 \n"
        entries = _parse_page_entries(text)
        assert entries[0].number == "2" and entries[0].title == "主要符号" and entries[0].printed_page == 2
        assert entries[1].number == "3.1" and entries[1].printed_page == 5

    def test_separate_page_line(self):
        text = "目 录\n3 管道结构上的作用……………………………………………\n5\n4 基本设计规定\n4.1 一般规定\n10\n"
        entries = _parse_page_entries(text)
        assert (entries[0].number, entries[0].printed_page) == ("3", 5)
        assert entries[1].number == "4" and entries[1].printed_page is None
        assert (entries[2].number, entries[2].printed_page) == ("4.1", 10)

    def test_watermark_rejected(self):
        text = "目次\n引用于《某规范》 2023年第一版 某出版社\n钢管购买热线：13337883086(微信同号)\n1 总则........... 1\n2 主要符号 ... 2\n"
        entries = _parse_page_entries(text)
        assert [e.number for e in entries] == ["1", "2"]  # 水印行被剔除

    def test_is_toc_page_messy(self):
        text = "目次\n1 总则……\n2 \n主要符号........... 2 \n3 \n管道结构上的作用……………………\n5\n3.1 \n作用分类和作用代表值\n5\n"
        assert is_toc_page(text, min_entries=3) is True

    def test_chinese_numeral_page_cleaned_not_extracted(self):
        """中文页码残留「·四」：从标题剔除但**不**当页码（防破坏单调性守卫）。"""
        entries = _parse_page_entries("目次\n5 基本构造要求……………........…………………·四\n6 验收…………………………………十五\n")
        assert entries[0].number == "5" and entries[0].printed_page is None
        assert entries[0].title == "基本构造要求"
        assert entries[1].number == "6" and entries[1].printed_page is None

    def test_appendix_not_merged_with_bare_number(self):
        """附录条目无数字编号：裸数字在其前是页码，不被当成附录的编号。"""
        entries = _parse_page_entries(
            "目次\n附录A\n管侧回填土的综合变形模量\n21\n附录B\n管顶竖向土压力标准值\n23\n"
        )
        assert not any(e.number in ("21", "23") for e in entries)  # 21/23 是页码非编号

    def test_appendix_entries_parsed(self):
        """附录 A-E（无编号 + 标签/标题/页码独立行）→ 解析为条目，不再被整体丢弃。"""
        entries = _parse_page_entries(
            "目次\n附录A\n管侧回填土的综合变形模量\n21\n附录B\n管顶竖向土压力标准值\n23\n"
        )
        assert len(entries) == 2
        assert entries[0].number == "" and entries[0].title == "附录A 管侧回填土的综合变形模量"
        assert entries[0].printed_page == 21
        assert entries[1].title == "附录B 管顶竖向土压力标准值"
        assert entries[1].printed_page == 23

    def test_appendix_without_page_kept(self):
        """附录无页码（罕见）→ 仍保留为条目（page=None），不被丢弃。"""
        entries = _parse_page_entries("目次\n附录A\n管侧回填土的综合变形模量\n附录B\n管顶竖向土压力标准值\n")
        assert [e.title for e in entries] == ["附录A 管侧回填土的综合变形模量", "附录B 管顶竖向土压力标准值"]


class TestContinuesToc:
    """目录续页判定：无关键词 + 编号连续性（防条文说明/前言清单页误判为目录）。"""

    def test_continuation_new_level1(self):
        """真实多页目录续页：上一页末条 1.3 → 本页首条 2（新一级）→ True。"""
        assert continues_toc("2 应急保障 ... 5\n2.1 通信网络 ... 6", "1.3") is True

    def test_continuation_same_parent(self):
        """上一页末条 1.3 → 本页首条 1.4 → True。"""
        assert continues_toc("1.4 后勤保障 ... 4\n2 应急保障 ... 5", "1.3") is True

    def test_listing_restart_rejected(self):
        """条文说明清单页：编号重启（目录末条 31 → 清单首条 3）→ 拒绝，防误跳正文页。"""
        assert continues_toc("3 管道结构上的作用 ... 31\n3.1 一般规定 ... 31", "31") is False
        assert continues_toc("3 管道结构上的作用 ... 31\n3.1 一般规定 ... 31", "5") is False

    def test_insufficient_entries_rejected(self):
        assert continues_toc("1 总则 ... 4", "1.3") is False  # 仅 1 条

    def test_no_prev_number_rejected(self):
        assert continues_toc("2 应急保障 ... 5", None) is False


class TestAlignAndCollect:
    def test_align_pages(self):
        toc = SimpleNamespace(
            entries=[
                TocEntry(number="1", title="总则", printed_page=1, level=1),
                TocEntry(number="2", title="保障", printed_page=5, level=1),
            ],
            offset=None,
        )
        align_pages(toc, offset=3)
        assert [e.physical_page for e in toc.entries] == [4, 8]
        align_pages(toc, offset=None)  # 无偏移不动
        assert toc.entries[0].physical_page == 4

    def test_collect_merges_in_page_order(self):
        page_texts = {
            2: "目 录\n1. 总则... 1\n1.1 适用范围 ... 2",
            3: "2 应急保障 ... 5\n3.1 通信网络 ... 9",
        }
        entries = collect_toc_entries(page_texts)
        assert [e.number for e in entries] == ["1", "1.1", "2", "3.1"]
