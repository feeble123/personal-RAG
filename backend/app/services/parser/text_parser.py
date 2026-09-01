"""Markdown / 纯文本解析：md 按 # 标题层级切分，txt 按段落。"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.parser.base import DocumentParser, ParsedBlock, ParsedDocument

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _read_text(path: Path) -> str:
    """按编码尝试读取（utf-8 优先，兜底 gbk）。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


class MarkdownParser(DocumentParser):
    extensions = ("md", "markdown")

    def parse(
        self, path: Path, filename: str, chunk_strategy: str = "old", parse_mode: str = "fast"
    ) -> ParsedDocument:
        content = _read_text(path)
        quality: dict = {"parser": "markdown", "headings": 0, "paragraphs": 0}
        blocks: list[ParsedBlock] = []
        section_stack: list[str] = []

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _MD_HEADING.match(stripped)
            if m:
                level = len(m.group(1))
                title = m.group(2).strip()
                # 章节路径：按级别维护栈
                if len(section_stack) >= level:
                    section_stack = section_stack[: level - 1]
                section_stack.append(title)
                quality["headings"] += 1
                blocks.append(
                    ParsedBlock(text=title, section="/".join(section_stack[:-1]) or None, block_type="heading")
                )
            else:
                quality["paragraphs"] += 1
                blocks.append(
                    ParsedBlock(
                        text=stripped,
                        section="/".join(section_stack) or None,
                        block_type="paragraph",
                    )
                )

        quality["blocks"] = len(blocks)
        # P1-1：md 标题按编号估层级（blocks 未存 # 数量），正文无层级
        elements = [
            b.to_element(i, "markdown", heading_level=_text_heading_level(b))
            for i, b in enumerate(blocks)
        ]
        return ParsedDocument(blocks=blocks, quality=quality, elements=elements)


def _text_heading_level(block: ParsedBlock) -> int | None:
    """md 标题块层级（IR 用）：用编号模式估（与 PDF/docx 一致）。"""
    if block.block_type != "heading":
        return None
    from app.services.parser.headings import heading_level

    lvl = heading_level(block.text)
    return lvl if lvl >= 1 else None


class TextParser(DocumentParser):
    extensions = ("txt",)

    def parse(
        self, path: Path, filename: str, chunk_strategy: str = "old", parse_mode: str = "fast"
    ) -> ParsedDocument:
        content = _read_text(path)
        quality: dict = {"parser": "text", "paragraphs": 0}
        blocks: list[ParsedBlock] = []

        for para in re.split(r"\n\s*\n", content):
            para = " ".join(ln.strip() for ln in para.splitlines() if ln.strip()).strip()
            if para:
                blocks.append(ParsedBlock(text=para, block_type="paragraph"))
                quality["paragraphs"] += 1

        quality["blocks"] = len(blocks)
        elements = [b.to_element(i, "text") for i, b in enumerate(blocks)]
        return ParsedDocument(blocks=blocks, quality=quality, elements=elements)
