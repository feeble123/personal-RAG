"""会话模块 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas import CitationOut, ORMModel


class ConversationCreate(BaseModel):
    title: str | None = Field(None, max_length=100)


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)


class ConversationOut(ORMModel):
    id: int
    title: str
    last_message_at: datetime
    created_at: datetime


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    total: int


class MessageOut(ORMModel):
    id: int
    conversation_id: int
    role: str
    content: str
    is_complete: bool
    error: str | None = None
    # 问答记忆库：反馈 + 来源标记 + 检索作用域（反馈时读取）
    feedback: str | None = None
    from_memory: bool = False
    kb_id: int | None = None
    doc_scope: str | None = None
    style: str | None = None
    created_at: datetime
    # 引用随消息返回（历史会话可还原）
    citations: list[CitationOut] = []


class MessageListOut(BaseModel):
    items: list[MessageOut]
    has_more: bool = False


class ChatIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    kb_id: int | None = None
    # 回答风格（单元 F）：standard/logical/summary/expanded/tutorial；缺省用知识库默认
    style: str | None = Field(None, max_length=30)
