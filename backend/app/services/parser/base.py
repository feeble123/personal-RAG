"""解析器接口与数据结构。

升级路径：任何新的文档格式 / 解析引擎只需实现 DocumentParser 接口，
在 factory 中注册即可，上层（chunker/入库）零改动。

P1-1：`ParsedDocument.elements` 为统一 DocumentElement IR（新增，增量接入）；
`blocks` 保留作兼容层（旧链路 chunker 仍走 blocks）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.parser.ir import DocumentElement, ElementType


@dataclass
class ParsedBlock:
    """一个可切分单元：段落 / 标题 / 表格。"""

    text: str
    section: str | None = None  # 章节路径，如 "第三章 明渠恒定流 / 3.1 均匀流"
    page: int | None = None     # 来源页码（1 起）
    block_type: str = "paragraph"  # paragraph / heading / table / figure

    def to_element(
        self,
        idx: int,
        parser: str,
        parser_version: str = "1",
        *,
        heading_level: int | None = None,
        flags: frozenset[str] = frozenset(),
        table: dict | None = None,
    ) -> DocumentElement:
        """P1-1：ParsedBlock → DocumentElement（兼容 adapter）。

        旧块无 bbox/confidence/reading_order——PDF 栈模式的标题是推断的，
        标注 `inferred_heading` flag（不假装精确）。section 转 section_path。
        """
        btype = self.block_type or "paragraph"
        if btype == "heading":
            etype = ElementType.HEADING
        elif btype == "table":
            etype = ElementType.TABLE
        elif btype == "figure":
            etype = ElementType.FIGURE
        else:
            etype = ElementType.PARAGRAPH

        path = tuple(p.strip() for p in (self.section or "").split("/") if p.strip())
        inferred = btype == "heading" and heading_level is None
        flags2 = set(flags)
        if inferred:
            flags2.add("inferred_heading")
        return DocumentElement(
            element_id=f"{parser}-{idx}",
            type=etype,
            text=self.text,
            page_start=self.page,
            page_end=self.page,
            reading_order=idx,
            heading_level=heading_level,
            section_path=path,
            table=table,
            source_ref={"parser": parser, "parser_version": parser_version, "block_index": idx},
            flags=frozenset(flags2),
        )


@dataclass
class ParsedDocument:
    """解析结果：块序列 + 质量指标（答辩数据）。"""

    blocks: list[ParsedBlock] = field(default_factory=list)
    page_count: int = 0
    quality: dict[str, Any] = field(default_factory=dict)
    # 目录（TOC）权威大纲：PDF 解析器填充（toc.TocInfo），其余解析器留 None
    outline: Any = None
    # 目录页原文（物理页 → 页文本）：PDF 新策略下单独成「目录」切片，内容永不丢弃
    toc_texts: dict[int, str] = field(default_factory=dict)
    # P1-1：统一 IR（DocumentElement 列表）；解析器产出后填充，chunker 可选消费
    elements: list[DocumentElement] = field(default_factory=list)


class DocumentParser(ABC):
    """文档解析接口。"""

    #: 支持的扩展名（小写，不含点）
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, path: Path, filename: str, chunk_strategy: str = "old") -> ParsedDocument:  # noqa: ARG002
        """解析文件为块序列。filename 用于判断真实类型（如 doc 旧格式）。

        chunk_strategy：切片策略（old=经典启发式 / new=目录+LLM断号补全）。
        PDF 解析器据此决定是否启用目录页识别与大纲提取；其余解析器忽略。
        """
        raise NotImplementedError
