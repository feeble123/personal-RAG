"""跨模块通用 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CitationOut(ORMModel):
    """引用/证据回传结构：问答时前端据此渲染引用卡片。

    P0-5：chunk_id 可空——重灌/删文档后历史引用的 chunk 已删，快照字段仍可显示。
    """

    chunk_id: int | None = None
    kb_id: int | None = None
    doc_id: int | None = None
    source: str
    page: int | None = None
    section: str | None = None
    snippet: str
    score: float | None = None
    rank: int | None = None


class PageMeta(BaseModel):
    page: int = 1
    page_size: int = 20
    total: int = 0
