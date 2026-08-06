"""问答记忆库服务（AI native 自身长库）：用户背书(👍/👎)沉淀的长期问答记忆。

完全独立于 RAG 知识库（chunks/Chroma），作为叠加在检索之上的「经验快通道」。
- 按用户隔离（user_id）；命中须检索作用域一致（kb_id/doc_scope/style）且主题一致。
- status='good' 命中 → 直接复用记忆答案；status='bad' 命中 → 强制重新检索信号
  （调用方应跳过记忆复用与语义缓存，走正常 RAG）。
- **通用可复用组件**：只依赖 QaMemory ORM + 标准库；阈值/容量经 MemoryConfig 注入；
  向量与主题词由调用方（路由）算出传入。整文件可拷贝到 agent 项目复用。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QaMemory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryConfig:
    """记忆库配置（可移植：默认值独立于应用 settings，agent 复用时可整体带参数）。"""

    enabled: bool = True
    threshold: float = 0.93  # 严格复用阈值：近似同题才复用
    max_entries: int = 300  # 每用户每 kb 作用域上限
    pool: int = 100  # 召回候选池（最近 N 条做余弦比对）
    eviction_ratio: float = 0.2  # 容量超限淘汰比例


@dataclass
class RecallResult:
    memory_id: int
    status: str  # good / bad
    answer: str | None
    citations: list[dict]
    score: float
    force_rerank: bool = False  # bad 命中 → 强制重新检索信号


def _cosine(a: list[float], b: list[float]) -> float:
    n = len(a)
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _subjects_match(a: str | None, b: str | None) -> bool:
    """主题一致性：余弦相似 ≠ 同一问题（同框架不同主题），须主题一致才允许命中。
    任一侧缺主题时保守放行。"""
    if not a or not b:
        return True
    return a == b or a in b or b in a


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scope_stmt(user_id: int, kb_id: int | None, doc_scope: str | None, style: str | None):
    """作用域 SQL 过滤：各维度「NULL 或等值」（同 SemanticCache.find）。"""
    stmt = select(QaMemory).where(QaMemory.user_id == user_id)
    if kb_id is None:
        stmt = stmt.where(QaMemory.kb_id.is_(None))
    else:
        stmt = stmt.where(QaMemory.kb_id == kb_id)
    if doc_scope is None:
        stmt = stmt.where(QaMemory.doc_scope.is_(None))
    else:
        stmt = stmt.where(QaMemory.doc_scope == doc_scope)
    if style is None:
        stmt = stmt.where(QaMemory.style.is_(None))
    else:
        stmt = stmt.where(QaMemory.style == style)
    return stmt


async def recall(
    db: AsyncSession,
    query_vector: list[float],
    subject: str | None,
    *,
    user_id: int,
    kb_id: int | None = None,
    doc_scope: str | None = None,
    style: str | None = None,
    config: MemoryConfig | None = None,
) -> RecallResult | None:
    """召回记忆：同用户同作用域候选池中找余弦 ≥ 阈值且主题一致的条目。

    bad 命中优先于 good（负面信号压过正向）；命中返回 answer/citations，
    bad 命中额外置 force_rerank=True 供调用方强制重新检索。
    """
    cfg = config or MemoryConfig()
    if not cfg.enabled:
        return None
    stmt = _scope_stmt(user_id, kb_id, doc_scope, style).order_by(
        QaMemory.updated_at.desc()
    ).limit(cfg.pool)
    rows = (await db.execute(stmt)).scalars().all()
    hits: list[tuple[QaMemory, float]] = []
    for row in rows:
        try:
            sim = _cosine(query_vector, json.loads(row.question_vector_json))
        except Exception:
            continue
        if sim >= cfg.threshold and _subjects_match(subject, row.subject):
            hits.append((row, sim))
    if not hits:
        return None
    bad = [h for h in hits if h[0].status == "bad"]
    chosen, chosen_sim = max(bad or hits, key=lambda h: h[1])
    chosen.hit_count += 1
    chosen.score = chosen_sim
    chosen.updated_at = _now()
    await db.commit()
    logger.debug("记忆召回 mem=%s status=%s sim=%.3f scope=%s/%s style=%s",
                 chosen.id, chosen.status, chosen_sim, chosen.kb_id, chosen.doc_scope, chosen.style)
    return RecallResult(
        memory_id=chosen.id,
        status=chosen.status,
        answer=chosen.answer,
        citations=json.loads(chosen.citations_json),
        score=chosen_sim,
        force_rerank=(chosen.status == "bad"),
    )


async def remember(
    db: AsyncSession,
    query_vector: list[float],
    subject: str | None,
    question: str,
    answer: str,
    citations: list[dict],
    *,
    user_id: int,
    kb_id: int | None = None,
    doc_scope: str | None = None,
    style: str | None = None,
    status: str = "good",
    score: float | None = None,
    config: MemoryConfig | None = None,
) -> None:
    """沉淀记忆：同作用域去重（相似问题更新现有而非重复）；状态纠偏（good↔bad 覆盖）；
    容量按 (user_id, kb_id) 计数，超限淘汰最旧 eviction_ratio。"""
    cfg = config or MemoryConfig()
    if not cfg.enabled or not answer:
        return

    # 去重：同作用域候选池找相似问题
    stmt = _scope_stmt(user_id, kb_id, doc_scope, style).order_by(
        QaMemory.updated_at.desc()
    ).limit(cfg.pool)
    dup: QaMemory | None = None
    for row in (await db.execute(stmt)).scalars().all():
        try:
            sim = _cosine(query_vector, json.loads(row.question_vector_json))
        except Exception:
            continue
        if sim >= cfg.threshold and _subjects_match(subject, row.subject):
            dup = row
            break
    if dup is not None:
        if dup.status == status:
            # 相似问题 → 更新现有（覆盖问题/答案/引用/向量/主题），而非重复
            dup.question = question
            dup.answer = answer
            dup.citations_json = json.dumps(citations, ensure_ascii=False)
            dup.question_vector_json = json.dumps(query_vector)
            dup.subject = subject
            dup.score = score
            dup.updated_at = _now()
            await db.commit()
            return
        # 状态纠偏：👍 纠正历史 👎（或 👎 否定历史 👍）→ 删旧插新
        await db.delete(dup)

    # 容量：按 (user_id, kb_id) 计数，超限淘汰最旧
    kb_cond = QaMemory.kb_id.is_(None) if kb_id is None else QaMemory.kb_id == kb_id
    total = (
        await db.scalar(
            select(func.count()).select_from(QaMemory).where(QaMemory.user_id == user_id, kb_cond)
        )
    ) or 0
    if total >= cfg.max_entries:
        kill = max(1, int(cfg.max_entries * cfg.eviction_ratio))
        ids = (
            await db.execute(
                select(QaMemory.id)
                .where(QaMemory.user_id == user_id, kb_cond)
                .order_by(QaMemory.updated_at)
                .limit(kill)
            )
        ).scalars().all()
        if ids:
            await db.execute(delete(QaMemory).where(QaMemory.id.in_(ids)))

    db.add(
        QaMemory(
            user_id=user_id,
            kb_id=kb_id,
            doc_scope=doc_scope,
            style=style,
            status=status,
            question=question,
            question_vector_json=json.dumps(query_vector),
            subject=subject,
            answer=answer,
            citations_json=json.dumps(citations, ensure_ascii=False),
            score=score,
        )
    )
    await db.commit()
    logger.info("记忆沉淀 user=%s kb=%s status=%s q=%.30s", user_id, kb_id, status, question)


async def record_feedback(
    db: AsyncSession,
    *,
    user_id: int,
    question: str,
    answer: str,
    citations: list[dict],
    feedback: str,  # up / down
    query_vector: list[float],
    subject: str | None,
    kb_id: int | None = None,
    doc_scope: str | None = None,
    style: str | None = None,
    config: MemoryConfig | None = None,
) -> bool:
    """反馈 → 记忆：up→good，down→bad。调用方负责算向量与主题词（本模块不依赖具体 embedding）。"""
    cfg = config or MemoryConfig()
    if not cfg.enabled or feedback not in ("up", "down"):
        return False
    await remember(
        db, query_vector, subject, question, answer, citations,
        user_id=user_id, kb_id=kb_id, doc_scope=doc_scope, style=style,
        status="good" if feedback == "up" else "bad", config=cfg,
    )
    return True


async def clear_memory() -> None:
    """清空全部记忆（仅测试/管理用；启动不清空——记忆是用户背书数据，与语义缓存不同）。"""
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        await db.execute(delete(QaMemory))
        await db.commit()
