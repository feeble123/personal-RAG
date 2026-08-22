"""大纲注入单元测试：硬注入（TOC 权威 1/2 级）/ 软注入（3/4/5 级）/ 段落拆分升格。"""
from __future__ import annotations

from app.services.chunker import StructureAwareChunker
from app.services.parser.base import ParsedBlock
from app.services.parser.gap_check import FoundNumber
from app.services.parser.outline import (
    Injection,
    apply_injections,
    demote_front_matter,
    demote_non_toc_headings,
    find_body_start,
    inject_blocks,
)
from app.services.parser.toc import TocEntry, TocInfo


def _entry(number, title, printed, level, physical):
    return TocEntry(number=number, title=title, printed_page=printed, level=level, physical_page=physical)


def _found(number, text, page, block_index, line_index=0):
    return FoundNumber(number=number, text=text, page=page, block_index=block_index, line_index=line_index)


class TestHardInjection:
    def test_missing_section_injected_by_page(self):
        """TOC 有 1.1 但正文无该标题 → 在 1.1 物理页首块前插入虚拟标题（section=None）。"""
        blocks = [
            ParsedBlock(text="1 总则", page=4, block_type="heading"),
            ParsedBlock(text="总则内容", page=4, block_type="paragraph"),
            ParsedBlock(text="适用范围的内容", page=5, block_type="paragraph"),
            ParsedBlock(text="1.2 组织指挥", page=5, block_type="heading"),
            ParsedBlock(text="组织内容", page=5, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[
                _entry("1", "总则", 1, 1, 4),
                _entry("1.1", "适用范围", 2, 2, 5),
                _entry("1.2", "组织指挥", 2, 2, 5),
            ],
            toc_pages=[2], offset=3, source="text",
        )
        out = inject_blocks(blocks, toc, set(), [])
        assert [b.text for b in out] == [
            "1 总则", "总则内容", "1.1 适用范围", "适用范围的内容", "1.2 组织指挥", "组织内容",
        ]
        injected = out[2]
        assert injected.block_type == "heading" and injected.section is None and injected.page == 5

    def test_hard_inject_all_toc_levels(self):
        """目录有几级就切到几级：三级目录条目也注入为硬标题边界。"""
        blocks = [
            ParsedBlock(text="1 总则", page=4, block_type="heading"),
            ParsedBlock(text="总则内容", page=4, block_type="paragraph"),
            ParsedBlock(text="1.1 范围的内容", page=5, block_type="paragraph"),
            ParsedBlock(text="1.1.1 一般规定的内容", page=6, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[
                _entry("1", "总则", 1, 1, 4),
                _entry("1.1", "范围", 2, 2, 5),
                _entry("1.1.1", "一般规定", 3, 3, 6),
            ],
            toc_pages=[2], offset=3, source="text",
        )
        out = inject_blocks(blocks, toc, set(), [])
        assert any(b.block_type == "heading" and b.text == "1.1 范围" for b in out)
        assert any(b.block_type == "heading" and b.text == "1.1.1 一般规定" for b in out)

    def test_injected_heading_becomes_stack_section(self):
        """注入后 chunker 仍走编号栈模式，1.1 成为真实章节边界。"""
        blocks = [
            ParsedBlock(text="1 总则", page=4, block_type="heading"),
            ParsedBlock(text="总则内容", page=4, block_type="paragraph"),
            ParsedBlock(text="适用范围的内容", page=5, block_type="paragraph"),
            ParsedBlock(text="1.2 组织指挥", page=5, block_type="heading"),
            ParsedBlock(text="组织内容", page=5, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[
                _entry("1", "总则", 1, 1, 4),
                _entry("1.1", "适用范围", 2, 2, 5),
                _entry("1.2", "组织指挥", 2, 2, 5),
            ],
            toc_pages=[2], offset=3, source="text",
        )
        out = inject_blocks(blocks, toc, set(), [])
        chunks = StructureAwareChunker(chunk_size=512).chunk(out)
        sections = [c.section for c in chunks]
        assert "1 总则 / 1.1 适用范围" in sections
        assert "1 总则 / 1.2 组织指挥" in sections

    def test_missing_section_injected_before_page_anchor(self):
        """有页码偏移时：在缺失 1.1 的物理页首块前插虚拟标题（段落不拆，位置准）。"""
        blocks = [
            ParsedBlock(text="1 总则", page=4, block_type="heading"),
            ParsedBlock(text="1.1 适用范围\n适用范围的内容", page=5, block_type="paragraph"),
            ParsedBlock(text="1.2 组织指挥", page=5, block_type="heading"),
        ]
        toc = TocInfo(
            entries=[
                _entry("1", "总则", 1, 1, 4),
                _entry("1.1", "适用范围", 2, 2, 5),
                _entry("1.2", "组织指挥", 2, 2, 5),
            ],
            toc_pages=[2], offset=3, source="text",
        )
        found = [_found("1.1", "1.1 适用范围", 5, 1, 0)]
        out = inject_blocks(blocks, toc, set(), found)
        assert [b.text for b in out] == [
            "1 总则",
            "1.1 适用范围",
            "1.1 适用范围\n适用范围的内容",  # 原段落保留（页码锚定优先，位置更准）
            "1.2 组织指挥",
        ]
        assert out[1].block_type == "heading" and out[1].section is None

    def test_hard_inject_split_when_no_page_anchor(self):
        """无页码偏移时：正文行首出现该编号且像标题 → 原地拆段落升格。"""
        from app.services.parser.outline import build_hard_injections

        blocks = [
            ParsedBlock(text="1 总则", page=4, block_type="heading"),
            ParsedBlock(text="1.1 适用范围\n适用范围的内容", page=5, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("1", "总则", None, 1, None), _entry("1.1", "适用范围", None, 2, None)],
            toc_pages=[2], offset=None, source="text",
        )
        found = [_found("1.1", "1.1 适用范围", 5, 1, 0)]
        injs = build_hard_injections(toc, blocks, found, body_start=4)
        splits = [x for x in injs if x.split_line_index is not None]
        assert len(splits) == 1 and splits[0].text == "1.1 适用范围"

    def test_page_anchor_prefers_title_line(self):
        """页内标题锚定：目标页里前一节尾巴在前、本节标题在后 → 边界插到标题行前。"""
        from app.services.parser.outline import build_hard_injections

        blocks = [
            ParsedBlock(text="4.1.5 对管道的结构设计应包括管体、管座。", page=18, block_type="paragraph"),
            ParsedBlock(text="承载能力极限状态计算规定", page=18, block_type="paragraph"),
            ParsedBlock(text="管道结构按承载能力极限状态进行强度计算时，应采用作用效应的基本组合。", page=18, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("4.2", "承载能力极限状态计算规定", 11, 2, 18)],
            toc_pages=[2], offset=7, source="text",
        )
        injs = build_hard_injections(toc, blocks, [], body_start=None)
        assert len(injs) == 1
        # 插在「承载能力极限状态计算规定」块（下标1）前，而非页首块（下标0）——4.1 尾巴不被吞
        assert injs[0].block_index == 1

    def test_page_anchor_title_off_by_one_page(self):
        """标题在物理页的下一页（页码容差 +1）：仍锚定到标题行，而非页首。"""
        from app.services.parser.outline import build_hard_injections

        blocks = [
            ParsedBlock(text="4.1 尾巴内容", page=18, block_type="paragraph"),
            ParsedBlock(text="承载能力极限状态计算规定", page=19, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("4.2", "承载能力极限状态计算规定", 11, 2, 18)],
            toc_pages=[2], offset=7, source="text",
        )
        injs = build_hard_injections(toc, blocks, [], body_start=None)
        assert len(injs) == 1
        assert injs[0].block_index == 1


class TestSoftInjection:
    def test_soft_inject_number_only_fallback(self):
        """确认缺失 1.2，正文/目录都找不到 → 插「仅编号」软边界到下一个兄弟之前。"""
        blocks = [
            ParsedBlock(text="1.1 组织保障", page=4, block_type="heading"),
            ParsedBlock(text="内容A", page=4, block_type="paragraph"),
            ParsedBlock(text="1.3 通信保障", page=6, block_type="heading"),
            ParsedBlock(text="内容B", page=6, block_type="paragraph"),
        ]
        found = [
            _found("1.1", "1.1 组织保障", 4, 0),
            _found("1.3", "1.3 通信保障", 6, 2),
        ]
        toc = TocInfo(entries=[], toc_pages=[], offset=None, source="text")
        out = inject_blocks(blocks, toc, {"1.2"}, found)
        soft = out[2]
        assert soft.block_type == "soft_heading" and soft.text == "1.2" and soft.section is None
        assert out[3].text == "1.3 通信保障"  # 插在其之前，保序

    def test_soft_inject_split_paragraph(self):
        """确认缺失编号在正文段落行首 → 拆段落插入软标题。"""
        blocks = [
            ParsedBlock(text="2 应急保障", page=4, block_type="heading"),
            ParsedBlock(text="2.1.3 现场处置\n现场处置的内容", page=6, block_type="paragraph"),
        ]
        found = [_found("2.1.3", "2.1.3 现场处置", 6, 1, 0)]
        toc = TocInfo(entries=[], toc_pages=[], offset=None, source="text")
        out = inject_blocks(blocks, toc, {"2.1.3"}, found)
        assert out[1].block_type == "soft_heading" and out[1].text == "2.1.3 现场处置"
        assert out[2].text == "现场处置的内容"


    def test_soft_inject_without_toc(self):
        """无目录（outline=None）时仍做软注入：3/4/5 级缺口靠兄弟连续性。"""
        blocks = [
            ParsedBlock(text="1.1 组织保障", page=4, block_type="heading"),
            ParsedBlock(text="1.3 通信保障", page=6, block_type="heading"),
        ]
        found = [
            _found("1.1", "1.1 组织保障", 4, 0),
            _found("1.3", "1.3 通信保障", 6, 1),
        ]
        out = inject_blocks(blocks, None, {"1.2"}, found)
        assert any(b.block_type == "soft_heading" and b.text == "1.2" for b in out)


class TestFrontMatter:
    def test_find_body_start(self):
        """正文起点 = 首个一级目录标题最早出现的页；封面碎片按标题不互含排除。"""
        blocks = [
            ParsedBlock(text="2. 中国工程建设标准化协会标准", page=2, block_type="heading"),
            ParsedBlock(text="1 总则", page=8, block_type="heading"),
            ParsedBlock(text="2 主要符号", page=9, block_type="heading"),
        ]
        toc = TocInfo(
            entries=[_entry("1", "总则", None, 1, None), _entry("2", "主要符号", 2, 1, None)],
            toc_pages=[7], offset=None, source="text",
        )
        assert find_body_start(toc, blocks) == 8

    def test_demote_front_matter_keeps_body_headings(self):
        """封面/前言标题降级为段落；正文标题保留。"""
        blocks = [
            ParsedBlock(text="封面标题", page=1, block_type="heading"),
            ParsedBlock(text="前言", page=2, block_type="heading"),
            ParsedBlock(text="1 总则", page=8, block_type="heading"),
        ]
        out = demote_front_matter(blocks, 8)
        assert [b.block_type for b in out] == ["paragraph", "paragraph", "heading"]
        assert out[2].text == "1 总则"

    def test_front_matter_not_polluting_stack(self):
        """注入+切块后：封面碎片不再污染章节前缀，正文从「1 总则」开始。"""
        blocks = [
            ParsedBlock(text="2. 中国工程建设标准化协会标准", page=2, block_type="heading"),
            ParsedBlock(text="封面出版信息", page=2, block_type="paragraph"),
            ParsedBlock(text="1 总则", page=8, block_type="heading"),
            ParsedBlock(text="总则内容", page=8, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("1", "总则", None, 1, None)], toc_pages=[7], offset=None, source="text"
        )
        out = inject_blocks(blocks, toc, set(), [])
        chunks = StructureAwareChunker(chunk_size=512).chunk(out)
        # 封面块成为无前缀普通 chunk；正文 chunk 前缀从「1 总则」开始
        body_chunks = [c for c in chunks if "总则内容" in c.content]
        assert body_chunks[0].section == "1 总则"
        assert not any("中国工程建设标准化协会标准 /" in (c.section or "") for c in chunks)


class TestNoOp:
    def test_no_outline_no_change(self):
        blocks = [ParsedBlock(text="正文", page=1, block_type="paragraph")]
        out = inject_blocks(blocks, None, set(), [])
        assert out is blocks

    def test_empty_confirmed_no_soft(self):
        blocks = [ParsedBlock(text="1 总则", page=1, block_type="heading")]
        toc = TocInfo(
            entries=[_entry("1", "总则", 1, 1, 1)], toc_pages=[0], offset=0, source="text"
        )
        out = inject_blocks(blocks, toc, set(), [])
        assert len(out) == 1  # 1 已在正文，无注入


class TestNoNumberEntries:
    """无编号 TOC 条目（条文说明/附录A…）：保留章节身份，不降级不丢失（只增不减）。"""

    def test_title_only_heading_confirmed(self):
        """「条文说明」无编号标题被目录按标题确认 → 保留为 heading（不降级）。"""
        blocks = [
            ParsedBlock(text="5 基本构造要求", page=20, block_type="heading"),
            ParsedBlock(text="构造内容", page=20, block_type="paragraph"),
            ParsedBlock(text="条文说明", page=31, block_type="heading"),
            ParsedBlock(text="条文说明内容", page=31, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[
                _entry("5", "基本构造要求", 20, 1, 27),
                _entry("", "条文说明", 31, 1, 38),
            ],
            toc_pages=[2], offset=7, source="text",
        )
        out = demote_non_toc_headings(blocks, toc)
        assert out[0].block_type == "heading"  # 编号条目不受影响
        assert out[2].block_type == "heading" and out[2].text == "条文说明"

    def test_inject_no_number_entry_at_page_anchor(self):
        """正文缺少「条文说明」标题 → 在物理页锚点注入标题边界。"""
        from app.services.parser.outline import build_hard_injections

        blocks = [
            ParsedBlock(text="5 基本构造要求", page=20, block_type="heading"),
            ParsedBlock(text="条文说明内容", page=38, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("", "条文说明", 31, 1, 38)],
            toc_pages=[2], offset=7, source="text",
        )
        injs = build_hard_injections(toc, blocks, [], body_start=None)
        assert len(injs) == 1
        assert injs[0].text == "条文说明" and injs[0].block_type == "heading"
        assert injs[0].block_index == 1  # 插在 page>=38 的首个正文块前

    def test_no_number_entry_already_in_body_skipped(self):
        """正文已有「条文说明」标题 → 不重复注入。"""
        from app.services.parser.outline import build_hard_injections

        blocks = [
            ParsedBlock(text="条文说明", page=38, block_type="heading"),
            ParsedBlock(text="条文说明内容", page=38, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("", "条文说明", 31, 1, 38)],
            toc_pages=[2], offset=7, source="text",
        )
        injs = build_hard_injections(toc, blocks, [], body_start=None)
        assert injs == []

    def test_appendix_heading_confirmed_by_label_prefix(self):
        """正文标题「附录A」被目录条目「附录A 管侧…」按标签前缀确认 → 保留为硬边界。"""
        blocks = [
            ParsedBlock(text="5 基本构造要求", page=20, block_type="heading"),
            ParsedBlock(text="附录A", page=28, block_type="heading"),
            ParsedBlock(text="附录内容", page=28, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[
                _entry("5", "基本构造要求", 21, 1, 28),
                _entry("", "附录A 管侧回填土的综合变形模量", 21, 1, 28),
            ],
            toc_pages=[2], offset=7, source="text",
        )
        out = demote_non_toc_headings(blocks, toc)
        assert out[1].block_type == "heading" and out[1].text == "附录A"

    def test_appendix_not_reinjected_when_in_body(self):
        """正文已有「附录A」标题 → 无编号条目按标签前缀跳过注入（不重复）。"""
        from app.services.parser.outline import build_hard_injections

        blocks = [
            ParsedBlock(text="附录A", page=28, block_type="heading"),
            ParsedBlock(text="附录内容", page=28, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[_entry("", "附录A 管侧回填土的综合变形模量", 21, 1, 28)],
            toc_pages=[2], offset=7, source="text",
        )
        injs = build_hard_injections(toc, blocks, [], body_start=None)
        assert injs == []

    def test_no_number_appendix_through_pipeline(self):
        """无编号附录条目走完整 inject_blocks → apply_injections：不崩、无重复（回归空编号排序）。"""
        blocks = [
            ParsedBlock(text="5 基本构造要求", page=20, block_type="heading"),
            ParsedBlock(text="附录内容", page=29, block_type="paragraph"),
        ]
        toc = TocInfo(
            entries=[
                _entry("5", "基本构造要求", 21, 1, 28),
                _entry("", "资料性附录农村供水工程数据编码", 26, 1, 29),
            ],
            toc_pages=[2], offset=7, source="text",
        )
        out = inject_blocks(blocks, toc, set(), [])
        texts = [b.text for b in out]
        assert "资料性附录农村供水工程数据编码" in texts
        assert texts.count("5 基本构造要求") == 1


class TestTocConfirmationStrict:
    """目录确认严格化：编号一致必须标题也一致——正文列表项不再被当 1/2 级标题。"""

    def test_body_list_item_not_confirmed_by_number_alone(self):
        """「2 正常使用极限状态:…」编号撞「2 主要符号」但标题不互含 → 不确认（正文列表项）。"""
        from app.services.parser.outline import _toc_confirmed_heading

        toc = TocInfo(
            entries=[
                _entry("1", "总则", 1, 1, 8),
                _entry("2", "主要符号", 2, 1, 9),
                _entry("4.2", "承载能力极限状态计算规定", 11, 2, 18),
            ],
            toc_pages=[7], offset=7, source="text",
        )
        assert not _toc_confirmed_heading(toc, "2 正常使用极限状态:对应于管道结构符合正常使用或耐")
        assert not _toc_confirmed_heading(toc, "1 对粘性土可取")
        assert not _toc_confirmed_heading(toc, "2 可变作用应包括地面人群荷载、地面堆积荷载、地面车")
        assert not _toc_confirmed_heading(toc, "3 构件内分布钢筋的混凝土净保护层厚度不应小于20mm o")

    def test_real_numbered_heading_confirmed(self):
        """真 1/2 级标题（编号+标题都一致）仍确认。"""
        from app.services.parser.outline import _toc_confirmed_heading

        toc = TocInfo(
            entries=[
                _entry("4.2", "承载能力极限状态计算规定", 11, 2, 18),
                _entry("4.3", "正常使用极限状态验算规定", 14, 2, 21),
            ],
            toc_pages=[7], offset=7, source="text",
        )
        assert _toc_confirmed_heading(toc, "4.2 承载能力极限状态计算规定")
        assert _toc_confirmed_heading(toc, "4.3 正常使用极限状态验算规定")

    def test_short_exact_title_confirmed(self):
        """短标题精确相等仍确认（「1 总则」=「总则」，防被降级后重复注入）。"""
        from app.services.parser.outline import _toc_confirmed_heading

        toc = TocInfo(
            entries=[_entry("1", "总则", None, 1, None), _entry("2", "主要符号", 2, 1, None)],
            toc_pages=[7], offset=None, source="text",
        )
        assert _toc_confirmed_heading(toc, "1 总则")
        assert _toc_confirmed_heading(toc, "2 主要符号")

    def test_demote_body_list_item_heading(self):
        """正文列表项标题不再被目录确认 → demote 降级（不再是章节硬边界）。"""
        blocks = [
            ParsedBlock(text="4 基本设计规定", page=12, block_type="heading"),
            ParsedBlock(text="2 正常使用极限状态:对应于管道结构符合正常使用或耐", page=17, block_type="heading"),
        ]
        toc = TocInfo(
            entries=[
                _entry("4", "基本设计规定", 10, 1, 17),
                _entry("2", "主要符号", 2, 1, 9),
            ],
            toc_pages=[7], offset=7, source="text",
        )
        out = demote_non_toc_headings(blocks, toc)
        assert out[0].block_type == "heading"  # 真 1/2 级保留
        assert out[1].block_type != "heading"  # 正文列表项降级，不再进章节栈


class TestFormulaFragments:
    def test_formula_fragment_demoted_to_paragraph(self):
        """公式符号行（式中/Gik一一第/t 一一设计壁厚）→ 降级为 paragraph（并入公式块，防碎片）。"""
        blocks = [
            ParsedBlock(text="式中", page=19, block_type="heading"),
            ParsedBlock(text="Gik一一第 i 个永久作用标准值;", page=19, block_type="heading"),
            ParsedBlock(text="t 一一设计壁厚", page=20, block_type="heading"),
        ]
        toc = TocInfo(entries=[], toc_pages=[], offset=None, source="text")
        out = demote_non_toc_headings(blocks, toc)
        assert all(b.block_type == "paragraph" for b in out)

    def test_real_title_stays_soft_heading(self):
        """无编号未确认小节（条文说明内部「承载能力极限状态计算」等）→ soft_heading（软断点）。"""
        blocks = [
            ParsedBlock(text="承载能力极限状态计算", page=20, block_type="heading"),
            ParsedBlock(text="总则", page=1, block_type="heading"),
        ]
        toc = TocInfo(entries=[], toc_pages=[], offset=None, source="text")
        out = demote_non_toc_headings(blocks, toc)
        assert all(b.block_type == "soft_heading" for b in out)

    def test_numbered_unconfirmed_becomes_paragraph(self):
        """有编号但未确认（正文列表项/非目录条款行）→ paragraph（纯内容，不进章节栈）。"""
        blocks = [
            ParsedBlock(text="2 正常使用极限状态:对应于管道结构符合正常使用或耐", page=17, block_type="heading"),
            ParsedBlock(text="4.1.5 对管道的结构设计应包括管体、管座。", page=18, block_type="heading"),
        ]
        toc = TocInfo(entries=[], toc_pages=[], offset=None, source="text")
        out = demote_non_toc_headings(blocks, toc)
        assert all(b.block_type == "paragraph" for b in out)


class TestMultiSplit:
    def test_two_splits_same_block_no_duplication(self):
        """同一块两条 split 注入：原始行只消费一次，无重复、顺序正确。"""
        blocks = [
            ParsedBlock(
                text="1.1 适用范围\n适用范围内容\n1.3 组织指挥", page=4, block_type="paragraph"
            ),
        ]
        injections = [
            Injection(
                text="1.1 适用范围", page=4, block_index=0, block_type="heading",
                split_line_index=0, level=2, number="1.1",
            ),
            Injection(
                text="1.3 组织指挥", page=4, block_index=0, block_type="heading",
                split_line_index=2, level=2, number="1.3",
            ),
        ]
        out = apply_injections(blocks, injections)
        assert [b.text for b in out] == ["1.1 适用范围", "适用范围内容", "1.3 组织指挥"]
        assert [b.text for b in out].count("适用范围内容") == 1  # 不重复

    def test_split_plus_page_anchor_same_block(self):
        """同块页锚插入 + 拆分：页锚标题在前、拆分段在后，内容无重复。"""
        blocks = [
            ParsedBlock(text="2 应急保障\n2.1 组织保障\n组织内容", page=4, block_type="paragraph"),
        ]
        injections = [
            Injection(
                text="1 总则", page=3, block_index=0, block_type="heading",
                level=1, number="1",
            ),
            Injection(
                text="2.1 组织保障", page=4, block_index=0, block_type="heading",
                split_line_index=1, level=2, number="2.1",
            ),
        ]
        out = apply_injections(blocks, injections)
        assert [b.text for b in out] == ["1 总则", "2 应急保障", "2.1 组织保障", "组织内容"]
