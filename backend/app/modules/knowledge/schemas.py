"""知识库模块 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas import CitationOut, ORMModel


class KBCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    # 回答风格（单元 F）：standard/logical/summary/expanded/tutorial
    answer_style: str = Field("standard", max_length=30)


class KBUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    answer_style: str | None = Field(None, max_length=30)


class KBOut(ORMModel):
    id: int
    name: str
    description: str | None
    doc_count: int
    chunk_count: int
    status: str
    answer_style: str
    created_at: datetime

    @field_validator("answer_style", mode="before")
    @classmethod
    def _fill_answer_style(cls, v: object) -> object:
        """历史行（迁移加列未回填）answer_style 为 NULL/'' → 归一为 'standard'，避免列表接口 500。"""
        return v or "standard"


class DocumentVersionOut(ORMModel):
    """文档版本（P0-8）：展示重灌历史与当前 active，供答辩与审计。"""

    id: int
    status: str  # building / validated / active / failed / retired
    chunk_count: int
    source_hash: str | None
    error_message: str | None
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None


class DocumentOut(ORMModel):
    id: int
    kb_id: int
    filename: str
    file_type: str
    file_size: int
    status: str
    error_message: str | None
    page_count: int | None
    chunk_count: int
    quality: dict[str, Any] | None
    # 切片策略（上传时选择）：old=经典启发式 / new=目录+LLM断号补全
    chunk_strategy: str = "old"
    # P0-11 文档类型（未来 DSH 引用来源判断）：textbook/standard/manual/other
    doc_type: str = "other"
    created_at: datetime
    parsed_at: datetime | None
    # P0-8 版本历史（重灌审计）：最近若干版本
    versions: list[DocumentVersionOut] = []
    # 入库进度（解析中实时填充，如 OCR 页数进度）：{stage, done, total, percent}
    progress: dict[str, Any] | None = None


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    total: int


class UploadResult(BaseModel):
    id: int
    filename: str
    status: str = "pending"
    # P0-11 文档类型（上传时选择，落库回显）
    doc_type: str = "other"


class SearchResult(BaseModel):
    hits: list[CitationOut]
