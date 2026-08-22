"""全部 ORM 模型（SQLAlchemy 2.0，Mapped 声明式）。

12 张表：users / conversations / messages / knowledge_bases / documents
        / document_versions / index_versions / chunks / citations
        / embedding_cache / semantic_cache / qa_memory

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
    # ---- 证据等级（U3）：检索质量判级，用于拒答机制 + 检索质量分布报表 ----
    evidence_level: Mapped[str | None] = mapped_column(String(20), nullable=True)  # sufficient/partial/weak/none
    evidence_top_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 判级依据的 top1 分数
    # ---- 层2 完备性校验：枚举类问题是否答全（True=完整/False=触发补全重生成/None=未校验或非枚举）----
    answer_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # ---- LLM 优化（opt-in）：True = 用户点「🤖 LLM优化」产生的结果（刷新后可还原标签）----
    is_optimized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def optimized(self) -> bool:
        """LLM优化标记（Pydantic from_attributes 用属性名 optimized 映射 is_optimized 列）。"""
        return self.is_optimized

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
    # P0-8：当前可查询的索引版本指针（原子发布切点）；NULL 表示无索引。
    # 普通 Integer 指针（无 FK）：版本随库 CASCADE 删除，单版本无独立 GC，指针永不悬空。
    active_index_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="kb", cascade="all, delete-orphan", lazy="selectin"
    )
    # 索引版本随库级联删除；delete-orphan 配在"一"侧（IndexVersion.kb）
    index_versions: Mapped[list["IndexVersion"]] = relationship(
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
    # 切片策略（上传时选择，供策略 A/B 对比）：old=经典启发式 / new=目录+LLM断号补全
    chunk_strategy: Mapped[str] = mapped_column(String(10), default="old", nullable=False)
    # P0-11 文档类型（未来 DSH 引用来源判断）：textbook 教材 / standard 规范 / manual 手册 / other 其他
    # 上传时手动选择，随检索结果返回。旧数据默认 other。
    doc_type: Mapped[str] = mapped_column(String(20), default="other", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # P0-8：当前可查询的文档版本指针（原子发布切点）；NULL 表示尚无可用版本。
    # 普通 Integer 指针（无 FK）：版本随文档 CASCADE 删除，单版本无独立 GC，指针永不悬空。
    active_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    kb: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    # 删除文档时经 versions 级联删各版本的 chunks（delete-orphan 只在版本侧，避免多父冲突）
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="doc")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_documents_kb", "kb_id"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)  # 冗余便于整体删除
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # P0-8：所属文档版本（active/retired 均在版本下并存）。同版本内切片序号唯一。
    document_version_id: Mapped[int] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(String(300), nullable=True)  # 章节路径
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # P0-7：content_hash 不再唯一——同内容跨文档保留独立 chunk（来源正确、互不牵连）；
    # embedding 缓存仍按 content_hash 复用向量（见 EmbeddingCache）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # P0-11 检索出处元数据：块类型（text/table/formula/figure）、条款号、公式编号。
    # 全部可空（旧数据/解析器拿不到就 NULL），供未来 DSH 检索接口返回出处。
    block_type: Mapped[str | None] = mapped_column(String(10), nullable=True, default="text")
    clause_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    formula_no: Mapped[str | None] = mapped_column(String(30), nullable=True)

    doc: Mapped[Document] = relationship(back_populates="chunks")
    version: Mapped["DocumentVersion"] = relationship(back_populates="chunks")
    # 该 chunk 被引用的记录
    citations: Mapped[list["Citation"]] = relationship(back_populates="chunk", lazy="selectin")

    __table_args__ = (
        Index("ix_chunks_doc", "doc_id"),
        UniqueConstraint("document_version_id", "chunk_index", name="uq_chunks_ver_index"),
    )
    __mapper_args__ = {"confirm_deleted_rows": False}


class DocumentVersion(Base):
    """文档版本（P0-8 不可变版本）：每次解析/重灌产生一个 target 版本。

    status: building（解析/embedding 中）→ validated（chunks 已写入）→ active（已发布可查）
            → failed（失败，旧版不受影响）→ retired（被新版替换，保留可回滚）
    """

    __tablename__ = "document_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="building", nullable=False)
    # 源文件 sha256（同一文件重灌可检测未变更，避免无意义发布）
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 解析/切片画像（答辩数据）：parser 名 + chunk_strategy + chunk 参数
    parser_profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chunk_profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    quality_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    document: Mapped[Document] = relationship(back_populates="versions")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="version", cascade="all, delete-orphan", lazy="selectin"
    )


class IndexVersion(Base):
    """索引版本（P0-8 影子索引）：每次全量重建产生一个 target 索引。

    当前单 collection 方案：physical_name 记录 Chroma collection 名（恒为 kb_chunks，
    影子切换时改名）。status 语义同 DocumentVersion。expected_count 与 actual_count
    核对一致后才允许发布（防止影子写入时静默丢向量）。
    """

    __tablename__ = "index_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="building", nullable=False)
    physical_name: Mapped[str] = mapped_column(String(100), default="kb_chunks", nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    kb: Mapped[KnowledgeBase] = relationship(back_populates="index_versions")


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # P0-5：引用不可变快照——chunk_id 可空 + SET NULL：重灌/删文档删除 chunk 时保留历史引用行，
    # 快照字段（source/page/section/snippet + doc_id）仍可完整显示
    chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"), index=True, nullable=True
    )
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
    # P0-3 用户作用域：缓存按用户隔离（避免跨用户重放他人答案）
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
