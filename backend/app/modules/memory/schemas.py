"""问答记忆库管理系统 Pydantic 模型（仅管理员）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas import ORMModel


class MemoryOut(ORMModel):
    """记忆条目（列表即含完整答案与引用，前端详情弹窗直接读行）。username/kb_name 由 join 补出。"""

    id: int
    user_id: int
    username: str | None = None
    kb_id: int | None = None
    kb_name: str | None = None
    doc_scope: str | None = None
    style: str | None = None
    status: str  # good / bad
    question: str
    subject: str | None = None
    answer: str
    citations: list[dict] = []
    hit_count: int
    score: float | None = None
    created_at: datetime
    updated_at: datetime


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    total: int


class MemoryStatusUpdate(BaseModel):
    """手动纠正记忆状态（good↔bad）。"""

    status: Literal["good", "bad"]


class MemoryCreate(BaseModel):
    """管理员手动录入记忆（不依赖用户👍）。"""

    question: str = Field(..., min_length=1, max_length=2000)
    answer: str = Field(..., min_length=1, max_length=8000)
    kb_id: int | None = None
    style: str | None = Field(None, max_length=30)
    citations: list[dict] = []


class MemoryBatchDelete(BaseModel):
    ids: list[int] = Field(..., min_length=1)
