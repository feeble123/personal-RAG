"""统一 DocumentElement 中间表示（IR）（P1-1）。

目标：所有解析器输出同一种可审计结构，chunker 不再从文本猜标题层级、
页码、表格结构。当前 `ParsedBlock` 保留作兼容层（旧链路 blocks 不变），
各 parser 增量产出 `elements`；`to_element` 把旧块转 IR（标记 inferred）。

结构说明：
- element_id：parser 生成的稳定 ID（f"{parser_name}-{idx}"）
- type：ElementType 枚举（title/heading/paragraph/list_item/table/table_row/...）
- bbox/confidence：PDF/OCR 有值，Office/文本无页面概念 → None
- flags：{boilerplate_candidate, inferred_heading, ...} 标记而非物理删除
- source_ref：{parser, parser_version, block_index} 出处可追溯（答辩数据）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ElementType(str, Enum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    FIGURE = "figure"
    CAPTION = "caption"
    FORMULA = "formula"
    HEADER = "header"
    FOOTER = "footer"


@dataclass(frozen=True)
class DocumentElement:
    """一个可审计的文档元素（IR 原子单元）。"""

    element_id: str
    type: ElementType
    text: str
    page_start: int | None = None
    page_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    reading_order: int = 0
    heading_level: int | None = None
    section_path: tuple[str, ...] = ()
    parent_id: str | None = None
    table: dict | None = None
    confidence: float | None = None
    source_ref: dict = field(default_factory=dict)
    flags: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        """序列化（snapshot 测试 / 存库用）。"""
        return {
            "element_id": self.element_id,
            "type": self.type.value,
            "text": self.text,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bbox": list(self.bbox) if self.bbox else None,
            "reading_order": self.reading_order,
            "heading_level": self.heading_level,
            "section_path": list(self.section_path),
            "parent_id": self.parent_id,
            "table": self.table,
            "confidence": self.confidence,
            "source_ref": self.source_ref,
            "flags": sorted(self.flags),
        }
