"""分块器单元测试：章节边界 / 上下文注入 / 表格独立成块 / 超长二次切分。"""
from __future__ import annotations

from app.services.chunker import StructureAwareChunker, _hash, _normalize
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


class TestHashing:
    def test_hash_deterministic(self):
        assert _hash("同一内容") == _hash("同一内容")

    def test_hash_differs_for_different_content(self):
        assert _hash("内容A") != _hash("内容B")

    def test_normalize_ignores_whitespace(self):
        assert _normalize("a  b\nc") == _normalize("abc")
        # 同一内容归一化后哈希一致 → 内容去重有效
        assert _hash("a  b\nc") == _hash("abc")
