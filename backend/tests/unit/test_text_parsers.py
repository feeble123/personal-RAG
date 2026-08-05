"""文本类解析器单元测试：Markdown / 纯文本 / 解析器工厂。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import BizError
from app.services.parser.factory import get_parser, is_supported
from app.services.parser.text_parser import MarkdownParser, TextParser


class TestMarkdownParser:
    def test_heading_section_path(self):
        md = "# 水利工程\n\n## 明渠均匀流\n\n正文内容一段。\n\n### 3.1 条件\n\n第二条正文。\n"
        p = Path("t.md")
        p.write_text(md, encoding="utf-8")
        try:
            parsed = MarkdownParser().parse(p, "t.md")
            blocks = parsed.blocks
            # 标题 + 正文
            kinds = [b.block_type for b in blocks]
            assert kinds.count("heading") == 3
            assert kinds.count("paragraph") == 2
            # 章节路径注入：第三条正文的 section 应为 "水利工程/明渠均匀流/3.1 条件"
            para2 = [b for b in blocks if b.block_type == "paragraph"][1]
            assert para2.section == "水利工程/明渠均匀流/3.1 条件"
        finally:
            p.unlink(missing_ok=True)

    def test_heading_stack_reset(self):
        md = "## A\n\ntext1\n\n# B\n\ntext2\n"
        p = Path("t.md")
        p.write_text(md, encoding="utf-8")
        try:
            parsed = MarkdownParser().parse(p, "t.md")
            paras = [b for b in parsed.blocks if b.block_type == "paragraph"]
            assert paras[0].section == "A"
            assert paras[1].section == "B"  # # B 重置到一级
        finally:
            p.unlink(missing_ok=True)


class TestTextParser:
    def test_paragraph_splitting(self):
        txt = "第一段文字。\n\n第二段文字。\n\n第三段。\n"
        p = Path("t.txt")
        p.write_text(txt, encoding="utf-8")
        try:
            parsed = TextParser().parse(p, "t.txt")
            paras = [b for b in parsed.blocks if b.block_type == "paragraph"]
            assert len(paras) == 3
            assert paras[0].text.startswith("第一段")
        finally:
            p.unlink(missing_ok=True)

    def test_gbk_encoding_fallback(self):
        p = Path("t.txt")
        p.write_bytes("中文编码测试内容".encode("gbk"))
        try:
            parsed = TextParser().parse(p, "t.txt")
            assert parsed.blocks[0].text.startswith("中文编码")
        finally:
            p.unlink(missing_ok=True)


class TestParserFactory:
    def test_dispatch_by_extension(self):
        assert is_supported("doc.pdf")
        assert is_supported("note.md")
        assert is_supported("data.xlsx")
        assert not is_supported("evil.exe")

    def test_get_parser_unknown_extension_raises(self):
        with pytest.raises(BizError):
            get_parser("file.exe")
