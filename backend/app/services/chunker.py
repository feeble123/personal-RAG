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
    # P0-11 块类型（出处元数据）：text / table / formula / figure。
    # 解析器块是表格 → table；正文/标题 → text；目录页 → text。
    block_type: str = "text"


def _normalize(text: str) -> str:
    """归一化：去除空白与换行差异，用于内容去重哈希。"""
    return re.sub(r"\s+", "", text)


def _hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _section_prefix(section: str | None) -> str:
    if not section:
        return ""
    return f"## {section}\n"


def _group_by_breaks(buffer: list[str], breaks: set[int], max_size: int) -> list[list[str]]:
    """按软边界把 buffer 切成组，再贪心合并相邻组（合并后 ≤ max_size）。

    软边界元素（软标题行）是**所在组的首元素**（软标题 + 其后内容同组），
    避免标题孤儿化（标题在前一组末尾、内容却进了下一组）；相邻组可合并保持 chunk
    粒度，超 max_size 的组交由 _emit 的 RecursiveCharacterTextSplitter 二次切（段落内不切断）。
    """
    groups: list[list[str]] = []
    cur: list[str] = []
    for idx, item in enumerate(buffer):
        if idx in breaks and cur:
            groups.append(cur)
            cur = []
        cur.append(item)
    if cur:
        groups.append(cur)
    merged: list[list[str]] = []
    acc: list[str] = []
    acc_len = 0
    for g in groups:
        glen = sum(len(x) for x in g)
        if acc and acc_len + glen <= max_size:
            acc.extend(g)
            acc_len += glen
        else:
            if acc:
                merged.append(acc)
            acc = list(g)
            acc_len = glen
    if acc:
        merged.append(acc)
    return merged


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
        buffer_page: int | None = None  # 当前 buffer 起始页（chunk 页号归属；页边界 flush 保证同页）
        # 软边界（LLM 断号补全的 3/4/5 级标题）：buffer 中元素下标，超长 flush 时优先在此断。
        # 不触发硬 flush、不更新章节栈 —— 保证「2.1.1—2.1.4 同块」按长度自适应切分。
        breaks: set[int] = set()
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

        def flush(block_type: str = "text"):
            nonlocal buffer, buffer_len, breaks, buffer_page
            if not buffer:
                buffer_page = None
                return
            if breaks:
                # 有软边界：按断点分组 → 贪心合并相邻组（合并后 ≤ 单块阈值）→ 每组 emit。
                # 软边界优先在此断，段落本身绝不切断；组内超长仍走 RecursiveCharacterTextSplitter。
                for group in _group_by_breaks(buffer, breaks, self.chunk_size * 1.5):
                    text = "\n".join(group).strip()
                    if text:
                        chunks.extend(self._emit(text, current_section, buffer_page, block_type))
            else:
                text = "\n".join(buffer).strip()
                if text:
                    chunks.extend(self._emit(text, current_section, buffer_page, block_type))
            buffer = []
            buffer_len = 0
            breaks = set()
            buffer_page = None

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

            if block.block_type == "soft_heading":
                # 软边界（降级的非目录标题 / LLM 断号补全的 3/4/5 级标题）：
                # **不强制断 chunk**——只有 1/2 级硬边界才单独切片（用户原则：
                # 其余大纲+正文放进切片内容）。软标题记为 buffer 内断点，缓冲超长
                # flush 时优先在此断（粒度靠长度切分，_group_by_breaks 相邻组可合并）。
                # 不更新章节栈（前缀仍由 1/2 级硬边界决定，软标题不污染栈）。
                page = block.page if block.page else current_page
                current_page = page
                if buffer and page != buffer_page:
                    flush()  # 页边界仍强制断（chunk 页号准确）
                breaks.add(len(buffer))  # 软标题作为该组首元素（防标题孤儿化）
                if not buffer:
                    buffer_page = page
                buffer.append(block.text)
                buffer_len += len(block.text)
                if buffer_len >= self.chunk_size:
                    flush()
                continue

            # 非标题块：解析器路径模式采用 block.section（全路径）
            if parser_mode and block.section and block.section != current_section:
                flush()
                current_section = block.section
                stack = []

            page = block.page if block.page else current_page
            if block.block_type == "table":
                flush()
                # 表格单独成块（行很多时按行二次切分）；标记 block_type=table（出处元数据）
                for t in self._emit(block.text, current_section, page, block_type="table"):
                    chunks.append(t)
                continue

            if block.block_type == "figure":
                flush()
                # 图片单独成块；标记 block_type=figure（出处元数据），与 table 同等待遇。
                # 图片块文本短（图注/占位），不参与正文 buffer 合并，保证块类型不丢失。
                for f in self._emit(block.text, current_section, page, block_type="figure"):
                    chunks.append(f)
                continue

            # 普通段落
            if buffer and page != buffer_page:
                flush()  # 页边界：内容不跨页合并，chunk 页号准确（第 27 页不再被吞进 28）
            current_page = page
            if not buffer:
                buffer_page = page
            buffer.append(block.text)
            buffer_len += len(block.text)
            if buffer_len >= self.chunk_size:
                flush()

        flush()
        return chunks

    def _emit(
        self, text: str, section: str | None, page: int | None, block_type: str = "text"
    ) -> list[Chunk]:
        prefixed = _section_prefix(section) + text
        if len(prefixed) <= self.chunk_size * 1.5:
            return [
                Chunk(
                    content=prefixed,
                    section=section,
                    page=page,
                    content_hash=_hash(prefixed),
                    block_type=block_type,
                )
            ]
        # 超长：二次切分
        pieces = self._splitter.split_text(text)
        out = []
        for p in pieces:
            p = p.strip()
            if not p:
                continue
            content = _section_prefix(section) + p
            out.append(
                Chunk(
                    content=content,
                    section=section,
                    page=page,
                    content_hash=_hash(content),
                    block_type=block_type,
                )
            )
        return out


def merge_tiny_chunks(chunks: list[Chunk], min_len: int = 40) -> list[Chunk]:
    """把极小碎片 chunk（<min_len，公式符号行/表格行/条款清单等）并入**同 section** 的下一个 chunk。

    减少碎片孤岛（用户：公式部分太碎）。仅同 section 才合并，避免跨节错标；
    去前缀拼接，保持单一「## section」前缀。
    """
    if len(chunks) < 2:
        return chunks

    def _body(c: Chunk) -> str:
        p = _section_prefix(c.section)
        return c.content[len(p):] if p and c.content.startswith(p) else c.content

    out: list[Chunk] = []
    for c in chunks:
        # 图片块独立不合并：figure 是独立语义单元（图注/占位），合并进正文会丢块类型
        if out and out[-1].block_type == "figure":
            out.append(c)
            continue
        if out and len(out[-1].content) < min_len and out[-1].section == c.section and c.block_type != "figure":
            prev = out.pop()
            body = _body(prev) + "\n" + _body(c)
            content = _section_prefix(c.section) + body if c.section else body
            out.append(
                Chunk(
                    content=content,
                    section=c.section,
                    page=c.page or prev.page,
                    content_hash=_hash(content),
                    # 合并后取「table 优先」（表格块合并进正文时仍算表格）
                    block_type="table" if (c.block_type == "table" or prev.block_type == "table") else "text",
                )
            )
        else:
            out.append(c)
    return out


def chunk_blocks(blocks: list[ParsedBlock]) -> list[Chunk]:
    return merge_tiny_chunks(StructureAwareChunker().chunk(blocks))


def chunk_toc_pages(toc_texts: dict[int, str]) -> list[Chunk]:
    """目录页原文 → 「目录」独立切片（内容完整保留，结构独立于正文）。

    只增不减：目录页文本不进正文块流（避免污染章节栈），但必须作为可检索的
    「目录」切片进入知识库——「规范有哪些章节、各在第几页」类问题直接命中。
    """
    chunks: list[Chunk] = []
    for pno in sorted(toc_texts):
        text = (toc_texts[pno] or "").strip()
        if not text:
            continue
        content = f"## 目录\n{text}"
        chunks.append(
            Chunk(content=content, section="目录", page=pno, content_hash=_hash(content))
        )
    return chunks
