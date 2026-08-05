"""结构感知分块。

- 以解析块的章节/标题为边界切分（不跨章节）
- 每块注入「章节路径」前缀上下文（提升检索命中 + 引用可回显出处）
- 表格单独成块；超长块用 RecursiveCharacterTextSplitter（中文分隔符）二次切分
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings
from app.services.parser.base import ParsedBlock
from app.services.parser.headings import heading_level

# 中文友好分隔符（优先级从高到低）
_SEPARATORS = ["\n\n", "\n", "。", "；", "，", " ", ""]


@dataclass
class Chunk:
    content: str
    section: str | None
    page: int | None
    content_hash: str


def _normalize(text: str) -> str:
    """归一化：去除空白与换行差异，用于内容去重哈希。"""
    return re.sub(r"\s+", "", text)


def _hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _section_prefix(section: str | None) -> str:
    if not section:
        return ""
    return f"## {section}\n"


class StructureAwareChunker:
    def __init__(
        self,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ):
        self.chunk_size = chunk_size or settings.chunk_size
        self.overlap = overlap if overlap is not None else settings.chunk_overlap
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.overlap,
            separators=_SEPARATORS,
            keep_separator=True,
        )

    def chunk(self, blocks: list[ParsedBlock]) -> list[Chunk]:
        """结构感知分块（用户方案 2026-08-04：1/2 级标题为硬边界，3/4 级按长度切）。

        - 结构来源自动判定：
          · 解析器路径模式（markdown/docx：标题带父路径 / 段落带 "/" 全路径）→ 沿用解析器路径
          · PDF 栈模式（PDF：标题块无 section）→ 按编号层级维护栈，生成层级前缀
            如「## 3 正文部分 / 3.2 引用标准」，保留 1/2 级标题内部含义
        - 3 级及以上条款不设标题（headings.heading_level 已排除），作为节内内容按长度切
        """
        chunks: list[Chunk] = []
        buffer: list[str] = []
        buffer_len = 0
        # 结构来源判定（预扫描）：
        # 有标题块 → markdown/docx 的标题块带「父路径 section」（# ## 层级 / Word Heading 样式）
        #           → 走解析器路径模式；PDF 的标题块永远无 section → 走编号栈模式。
        # 无标题块 → 段落自带 section 则走解析器路径模式（纯 section 流）。
        #
        # 注意：不能用「section 含 "/"」判定——标准编号如「GB/T 20000.2」自带斜杠，
        # 会让 PDF 段落误判为路径 → 走错模式、层级栈失效（实测 doc5 因此全扁平）。
        has_heading = any(b.block_type == "heading" for b in blocks)
        if has_heading:
            has_heading_section = any(b.block_type == "heading" and b.section for b in blocks)
            parser_mode = has_heading_section
        else:
            parser_mode = any(b.section for b in blocks)
        stack: list[str] = []
        current_section: str | None = None
        current_page: int | None = None

        def flush():
            nonlocal buffer, buffer_len
            if not buffer:
                return
            text = "\n".join(buffer).strip()
            if text:
                chunks.extend(self._emit(text, current_section, current_page))
            buffer = []
            buffer_len = 0

        for block in blocks:
            if block.block_type == "heading":
                flush()
                if parser_mode:
                    # 解析器路径模式：路径由段落/表格的 section 提供，标题仅作边界
                    current_page = block.page
                    continue
                # PDF 栈模式：编号层级 → 栈 → 层级前缀
                lvl = heading_level(block.text)
                if lvl <= 0:
                    lvl = len(stack) + 1  # 无编号放大标题 → 栈下一级
                stack = stack[: lvl - 1]
                stack.append(block.text.strip())
                current_section = " / ".join(stack)
                current_page = block.page
                continue

            # 非标题块：解析器路径模式采用 block.section（全路径）
            if parser_mode and block.section and block.section != current_section:
                flush()
                current_section = block.section
                stack = []

            page = block.page if block.page else current_page
            if block.block_type == "table":
                flush()
                # 表格单独成块（行很多时按行二次切分）
                for t in self._emit(block.text, current_section, page):
                    chunks.append(t)
                continue

            # 普通段落
            current_page = page
            buffer.append(block.text)
            buffer_len += len(block.text)
            if buffer_len >= self.chunk_size:
                flush()

        flush()
        return chunks

    def _emit(self, text: str, section: str | None, page: int | None) -> list[Chunk]:
        prefixed = _section_prefix(section) + text
        if len(prefixed) <= self.chunk_size * 1.5:
            return [Chunk(content=prefixed, section=section, page=page, content_hash=_hash(prefixed))]
        # 超长：二次切分
        pieces = self._splitter.split_text(text)
        out = []
        for p in pieces:
            p = p.strip()
            if not p:
                continue
            content = _section_prefix(section) + p
            out.append(Chunk(content=content, section=section, page=page, content_hash=_hash(content)))
        return out


def chunk_blocks(blocks: list[ParsedBlock]) -> list[Chunk]:
    return StructureAwareChunker().chunk(blocks)
