"""全部 ORM 模型（SQLAlchemy 2.0，Mapped 声明式）。

8 张表：users / conversations / messages / knowledge_bases / documents
        / chunks / citations / embedding_cache

关系统一 lazy="selectin"，避免 async 会话序列化时 MissingGreenlet。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # admin / user
    nickname: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), default="新会话", nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_conversations_user_last", "user_id", "last_message_at"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # token 用量（答辩数据）
    usage_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ---- 问答记忆库（反馈 + 来源标记 + 检索作用域）----
    feedback: Mapped[str | None] = mapped_column(String(10), nullable=True)  # up / down / NULL
    from_memory: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # 答案来自记忆复用
    kb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 该问答的检索作用域（反馈时读取）
    doc_scope: Mapped[str | None] = mapped_column(String(100), nullable=True)
    style: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_messages_conv_created", "conversation_id", "created_at"),)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 冗余统计（避免连表）
    doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="empty", nullable=False)  # empty/indexing/ready
    # 回答风格（单元 F）：standard 规范条文 / logical 专业论证 / summary 要点摘要 /
    # expanded 拓展延伸 / tutorial 通俗讲解。问答时按此风格组装 SYSTEM_PROMPT
    answer_style: Mapped[str] = mapped_column(String(30), default="standard", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="kb", cascade="all, delete-orphan", lazy="selectin"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)  # 相对 uploads/
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending / parsing / embedding / ready / failed
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 解析质量指标（扫描判定/OCR/乱码率等），答辩数据
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    kb: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="doc", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_documents_kb", "kb_id"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)  # 冗余便于整体删除
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(String(300), nullable=True)  # 章节路径
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    doc: Mapped[Document] = relationship(back_populates="chunks")
    # 该 chunk 被引用的记录
    citations: Mapped[list["Citation"]] = relationship(back_populates="chunk", lazy="selectin")

    __table_args__ = (
        Index("ix_chunks_doc", "doc_id"),
        UniqueConstraint("content_hash", name="uq_chunks_hash"),
    )
    __mapper_args__ = {"confirm_deleted_rows": False}


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False)
    kb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(255), default="", nullable=False)  # 冗余文件名
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(300), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="", nullable=False)  # 引用原文
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    message: Mapped[Message] = relationship(back_populates="citations")
    chunk: Mapped[Chunk] = relationship(back_populates="citations")

    __table_args__ = (Index("ix_citations_message", "message_id"),)


class EmbeddingCache(Base):
    """文档向量缓存：按内容哈希去重，重入库同内容秒回，省 embedding API 调用。"""

    __tablename__ = "embedding_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    vector_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 数组
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class SemanticCache(Base):
    """语义缓存：相似提问（余弦 > 阈值）直接回缓存答案 + 引用，秒回。"""

    __tablename__ = "semantic_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_vector_json: Mapped[str] = mapped_column(Text, nullable=False)  # 查询向量
    # 核心主题词（focus_rerank_query 提取）：缓存命中须主题一致，
    # 否则「关于X的要求」这类同框架不同主题的问题会因余弦>阈值而错误重放
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 检索作用域（BUG-B）：kb_id = 选库；doc_scope = 点名文档的排序逗号串
    #（如 "4" / "3,4" / NULL）。不同库/不同文档的同一问题答案不同，
    # 命中缓存必须作用域完全一致，否则切库后同问会重放旧库答案。
    kb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    doc_scope: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 回答风格（单元 F）：同题不同风格答案不同，缓存命中须风格一致
    style: Mapped[str | None] = mapped_column(String(30), nullable=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class QaMemory(Base):
    """问答记忆库（AI native 自身长库）：用户点赞(👍)沉淀的正向记忆 + 点踩(👎)沉淀的负面记忆。

    完全独立于 RAG 知识库（chunks/Chroma），作为叠加在检索之上的「经验快通道」。
    - 按用户隔离（user_id）：不同用户的问答记忆互不可见。
    - 命中须检索作用域一致（kb_id/doc_scope/style，同 SemanticCache 语义）且主题一致。
    - status='bad' 的负面记忆命中时，调用方应强制重新检索（跳过记忆复用与语义缓存）。
    """

    __tablename__ = "qa_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)  # 按用户隔离
    # 检索作用域（与 SemanticCache 同语义）：kb_id 选库；doc_scope 点名文档排序串；style 回答风格
    kb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_scope: Mapped[str | None] = mapped_column(String(100), nullable=True)
    style: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="good", nullable=False)  # good / bad
    question: Mapped[str] = mapped_column(Text, nullable=False)  # 原始用户问题
    question_vector_json: Mapped[str] = mapped_column(Text, nullable=False)  # 问题向量
    subject: Mapped[str | None] = mapped_column(String(200), nullable=True)  # focus_rerank_query 主题词
    answer: Mapped[str] = mapped_column(Text, default="", nullable=False)  # bad 时存被踩答案（审计）
    citations_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 最近命中时的余弦分
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_qa_memory_user_scope", "user_id", "kb_id", "updated_at"),)
