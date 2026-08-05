"""解析器接口与数据结构。

升级路径：任何新的文档格式 / 解析引擎只需实现 DocumentParser 接口，
在 factory 中注册即可，上层（chunker/入库）零改动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParsedBlock:
    """一个可切分单元：段落 / 标题 / 表格。"""

    text: str
    section: str | None = None  # 章节路径，如 "第三章 明渠恒定流 / 3.1 均匀流"
    page: int | None = None     # 来源页码（1 起）
    block_type: str = "paragraph"  # paragraph / heading / table


@dataclass
class ParsedDocument:
    """解析结果：块序列 + 质量指标（答辩数据）。"""

    blocks: list[ParsedBlock] = field(default_factory=list)
    page_count: int = 0
    quality: dict[str, Any] = field(default_factory=dict)


class DocumentParser(ABC):
    """文档解析接口。"""

    #: 支持的扩展名（小写，不含点）
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, path: Path, filename: str) -> ParsedDocument:  # noqa: ARG002
        """解析文件为块序列。filename 用于判断真实类型（如 doc 旧格式）。"""
        raise NotImplementedError
