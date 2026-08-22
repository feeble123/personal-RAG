"""分块器单元测试：章节边界 / 上下文注入 / 表格独立成块 / 超长二次切分。"""
from __future__ import annotations

from app.services.chunker import (
    Chunk,
    StructureAwareChunker,
    _hash,
    _normalize,
    chunk_toc_pages,
    merge_tiny_chunks,
)
from app.services.parser.base import ParsedBlock


def make_blocks(specs):
    """specs: [(block_type, text, section?, page?)]"""
    blocks = []
    for spec in specs:
        if len(spec) == 3:
            btype, text, section = spec
            page = None
        else:
            btype, text, section, page = spec
        blocks.append(ParsedBlock(text=text, section=section, page=page, block_type=btype))
    return blocks


class TestChunkerBasics:
    def test_paragraph_accumulation(self):
        """多个段落累积成块，长度未超阈值时合成一个 chunk。"""
        paras = [("paragraph", f"第{i}段内容" * 10, None) for i in range(3)]
        chunks = StructureAwareChunker().chunk(make_blocks(paras))
        assert len(chunks) == 1
        assert "第0段内容" in chunks[0].content and "第2段内容" in chunks[0].content

    def test_section_boundary_splits_chunk(self):
        """1/2 级标题变化时强制切分，不跨章节（PDF 栈模式）。"""
        blocks = make_blocks(
            [
                ("heading", "第一章", None),
                ("paragraph", "第一章内容" * 20, None),
                ("heading", "第二章", None),
                ("paragraph", "第二章内容" * 20, None),
            ]
        )
        chunks = StructureAwareChunker().chunk(blocks)
        assert len(chunks) == 2
        assert "第一章内容" in chunks[0].content and "第二章内容" in chunks[1].content

    def test_parser_path_section_splits_chunk(self):
        """解析器路径模式（markdown/docx 全路径）变化时切分。"""
        blocks = make_blocks(
            [
                ("paragraph", "第一章内容" * 20, "工程水文学/第一章"),
                ("paragraph", "第二章内容" * 20, "工程水文学/第二章"),
            ]
        )
        chunks = StructureAwareChunker().chunk(blocks)
        assert len(chunks) == 2
        assert "第一章内容" in chunks[0].content and "第二章内容" in chunks[1].content

    def test_pdf_hierarchical_prefix(self):
        """PDF 栈模式：1/2 级标题生成层级前缀，保留一二级标题含义。"""
        blocks = make_blocks(
            [
                ("heading", "3 正文部分", None),
                ("heading", "3.2 引用标准", None),
                ("paragraph", "引用标准清单书写格式应为：标准编号、标准名称。", None),
            ]
        )
        chunks = StructureAwareChunker().chunk(blocks)
        assert len(chunks) == 1
        assert chunks[0].content.startswith("## 3 正文部分 / 3.2 引用标准")
        assert chunks[0].section == "3 正文部分 / 3.2 引用标准"

    def test_section_prefix_injected(self):
        """chunk 内容注入章节前缀（上下文增强）。"""
        blocks = make_blocks([("paragraph", "正文内容", "3.1 均匀流")])
        chunks = StructureAwareChunker().chunk(blocks)
        assert chunks[0].content.startswith("## 3.1 均匀流")


class TestChunkerSpecial:
    def test_table_becomes_own_chunk(self):
        """表格单独成块，不与正文合并。"""
        blocks = make_blocks(
            [
                ("paragraph", "表格前正文", "第一章"),
                ("table", "a | b\n1 | 2", "第一章"),
                ("paragraph", "表格后正文", "第一章"),
            ]
        )
        chunks = StructureAwareChunker().chunk(blocks)
        # 正文两块 + 表格一块（按顺序）
        assert any("a | b" in c.content for c in chunks)
        table_chunk = next(c for c in chunks if "a | b" in c.content)
        assert "表格前正文" not in table_chunk.content
        assert "表格后正文" not in table_chunk.content

    def test_heading_updates_section(self):
        """heading 块更新后续段落的章节。"""
        blocks = make_blocks(
            [
                ("heading", "3.2 特性", None),
                ("paragraph", "特性正文", None),
            ]
        )
        chunks = StructureAwareChunker().chunk(blocks)
        assert len(chunks) == 1
        assert chunks[0].section == "3.2 特性"
        assert chunks[0].content.startswith("## 3.2 特性")

    def test_long_text_split(self):
        """超长文本被二次切分为多个 chunk，且不丢失内容。"""
        long_text = "明渠均匀流是重要的水流形态。" * 50  # 远超 chunk_size
        chunks = StructureAwareChunker(chunk_size=100, overlap=10).chunk(
            make_blocks([("paragraph", long_text, None)])
        )
        assert len(chunks) >= 2
        joined = "".join(c.content for c in chunks)
        assert "明渠均匀流是重要的水流形态" in joined

    def test_page_number_preserved(self):
        blocks = make_blocks([("paragraph", "带页码的段落", "第三章", 12)])
        chunks = StructureAwareChunker().chunk(blocks)
        assert chunks[0].page == 12


class TestChunkerSoftHeading:
    def test_soft_heading_not_forced_chunk_when_short(self):
        """软标题不强制起新 chunk：内容不长时 2.1/2.2 合成一个切片（其余大纲进内容）。"""
        blocks = make_blocks(
            [
                ("heading", "2 应急保障", None),
                ("soft_heading", "2.1 组织保障", None),
                ("paragraph", "组织内容" * 30, None),
                ("soft_heading", "2.2 通信保障", None),
                ("paragraph", "通信内容" * 30, None),
            ]
        )
        chunks = StructureAwareChunker(chunk_size=512).chunk(blocks)
        assert len(chunks) == 1  # 总长 < chunk_size，软边界不触发
        assert "2.1 组织保障" in chunks[0].content and "2.2 通信保障" in chunks[0].content
        # 软标题不污染章节前缀：整节仍挂 1/2 级 section
        assert chunks[0].section == "2 应急保障"

    def test_overflow_splits_at_soft_boundary(self):
        """每个软节内容超长 → 各成 chunk（软标题与其内容同块、不跨 chunk）。"""
        blocks = make_blocks(
            [
                ("heading", "2 应急保障", None),
                ("soft_heading", "2.1 组织保障", None),
                ("paragraph", "组织内容" * 150, None),
                ("soft_heading", "2.2 通信保障", None),
                ("paragraph", "通信内容" * 150, None),
            ]
        )
        chunks = StructureAwareChunker(chunk_size=512).chunk(blocks)
        assert len(chunks) == 2
        assert "2.1 组织保障" in chunks[0].content and "组织内容" in chunks[0].content
        assert "2.2 通信保障" not in chunks[0].content
        assert "2.2 通信保障" in chunks[1].content and "通信内容" in chunks[1].content
        assert "2.1 组织保障" not in chunks[1].content

    def test_soft_heading_does_not_change_section_stack(self):
        """软标题不更新章节栈：后续 chunk 仍挂 1/2 级 section 前缀。"""
        blocks = make_blocks(
            [
                ("heading", "2 应急保障", None),
                ("soft_heading", "2.1 组织保障", None),
                ("paragraph", "内容A" * 20, None),
            ]
        )
        chunks = StructureAwareChunker(chunk_size=512).chunk(blocks)
        assert len(chunks) == 1
        assert chunks[0].section == "2 应急保障"
        assert chunks[0].content.startswith("## 2 应急保障")


class TestChunkerPageBoundary:
    def test_page_change_flushes(self):
        """页边界 flush：不同页的段落不再合并，各自成 chunk 且页号准确。"""
        blocks = make_blocks(
            [
                ("paragraph", "第26页内容" * 10, None, 26),
                ("paragraph", "第27页内容" * 10, None, 27),
                ("paragraph", "第28页内容" * 10, None, 28),
            ]
        )
        chunks = StructureAwareChunker(chunk_size=512).chunk(blocks)
        assert [c.page for c in chunks] == [26, 27, 28]
        assert "第27页内容" in chunks[1].content

    def test_chunk_page_is_buffer_start(self):
        """chunk.page = buffer 起始页（不是末块页）。"""
        blocks = make_blocks(
            [
                ("paragraph", "起始内容" * 20, None, 5),
                ("paragraph", "后续内容" * 20, None, 5),
                ("paragraph", "下一页内容" * 20, None, 6),
            ]
        )
        chunks = StructureAwareChunker(chunk_size=512).chunk(blocks)
        assert chunks[0].page == 5 and "起始内容" in chunks[0].content
        assert chunks[1].page == 6

    def test_same_page_paragraphs_merge(self):
        """同页段落仍合并进同一 chunk（页边界不触发）。"""
        blocks = make_blocks(
            [
                ("paragraph", "甲" * 30, None, 5),
                ("paragraph", "乙" * 30, None, 5),
            ]
        )
        chunks = StructureAwareChunker(chunk_size=512).chunk(blocks)
        assert len(chunks) == 1
        assert chunks[0].page == 5


class TestMergeTinyChunks:
    def test_tiny_chunk_merged_into_next_same_section(self):
        chunks = [
            Chunk(content="## 5 基本构造要求\n式中", section="5 基本构造要求", page=19, content_hash="a"),
            Chunk(content="## 5 基本构造要求\nGik一一第 i 个永久作用标准值;", section="5 基本构造要求", page=19, content_hash="b"),
        ]
        out = merge_tiny_chunks(chunks)
        assert len(out) == 1
        assert "式中" in out[0].content and "Gik" in out[0].content
        assert out[0].content.count("## ") == 1  # 单一前缀，无重复
        assert out[0].section == "5 基本构造要求"

    def test_tiny_chunk_not_merged_across_sections(self):
        chunks = [
            Chunk(content="## 5 基本构造要求\n式中", section="5 基本构造要求", page=19, content_hash="a"),
            Chunk(content="## 4 基本设计规定\n大块内容", section="4 基本设计规定", page=20, content_hash="b"),
        ]
        out = merge_tiny_chunks(chunks)
        assert len(out) == 2  # 不同 section 不合并

    def test_untouched_when_no_tiny(self):
        chunks = [Chunk(content="正常内容" * 20, section=None, page=1, content_hash="a")]
        out = merge_tiny_chunks(chunks)
        assert len(out) == 1


class TestHashing:
    def test_hash_deterministic(self):
        assert _hash("同一内容") == _hash("同一内容")

    def test_hash_differs_for_different_content(self):
        assert _hash("内容A") != _hash("内容B")

    def test_normalize_ignores_whitespace(self):
        assert _normalize("a  b\nc") == _normalize("abc")
        # 同一内容归一化后哈希一致 → 内容去重有效
        assert _hash("a  b\nc") == _hash("abc")


class TestTocChunks:
    def test_each_toc_page_becomes_directory_chunk(self):
        """目录页原文单独成「目录」切片：内容完整保留、带目录标签与物理页。"""
        toc_texts = {2: "目 录\n1. 总则........... 1\n2 主要符号 ... 3", 3: "3 管道结构上的作用 ... 5"}
        chunks = chunk_toc_pages(toc_texts)
        assert len(chunks) == 2
        assert all(c.section == "目录" for c in chunks)
        assert all(c.content.startswith("## 目录\n") for c in chunks)
        assert chunks[0].page == 2 and chunks[1].page == 3
        assert "总则" in chunks[0].content and "管道结构上的作用" in chunks[1].content

    def test_empty_pages_skipped(self):
        chunks = chunk_toc_pages({2: "", 3: "   "})
        assert chunks == []
