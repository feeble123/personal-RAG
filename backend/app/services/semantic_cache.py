"""语义缓存服务：相似提问（余弦 > 阈值）直接回缓存答案 + 引用。

- 命中缓存返回 (answer, citations) 或 None
- 存储时控制容量（超限淘汰最旧），命中计数供答辩数据
- 作用域键：user_id + kb_id + doc_scope + style（P0-3：按用户隔离，跨用户不重放他人答案）
- TTL：超出 semantic_cache_ttl_seconds 的条目不命中，顺带清理过期（P0-3）
- 重要：缓存必须在启动时和文档入库/删除后清空——否则旧检索产生的错误答案
  会因「相同问题向量命中」而重放，绕过新检索逻辑（曾导致修复后问答仍错误）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import SemanticCache

logger = logging.getLogger(__name__)


async def clear_cache() -> None:
    """清空全部语义缓存（启动时 / 文档变更时调用，防旧答案残留）。"""
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        await db.execute(delete(SemanticCache))
        await db.commit()


def _cosine(a: list[float], b: list[float]) -> float:
    n = len(a)
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _subjects_match(a: str | None, b: str | None) -> bool:
    """缓存命中的主题一致性校验。

    余弦相似度高 ≠ 同一问题：如「关于补充部分的要求」与「关于编排格式的要求」
    共享问句框架，BGE 向量余弦 0.94 远超阈值，但主题不同，答案必然不同。
    只有主题一致（相等或互相包含）才允许命中。任一侧缺主题时保守放行。
    """
    if not a or not b:
        return True
    return a == b or a in b or b in a


def _is_expired(row: SemanticCache) -> bool:
    """TTL 过期判定：超过 semantic_cache_ttl_seconds 的条目不命中（0=永不过期）。"""
    ttl = settings.semantic_cache_ttl_seconds
    if ttl <= 0 or row.updated_at is None:
        return False
    return (_now() - row.updated_at) > timedelta(seconds=ttl)


async def find(
    db: AsyncSession,
    query_vector: list[float],
    subject: str | None = None,
    kb_id: int | None = None,
    doc_scope: str | None = None,
    style: str | None = None,
    user_id: int | None = None,
) -> tuple[str, list[dict]] | None:
    """在最近缓存池中找相似、主题一致且**检索作用域一致**的提问。命中返回 (answer, citations)。

    作用域（user_id + kb_id + doc_scope + style）须与缓存条目完全一致：切库/切换点名文档/
    改回答风格/换用户后，同一问题的向量余弦仍可能 > 阈值，但答案应不同，不得重放旧作用域的缓存。
    超出 TTL 的旧条目不命中（防重灌后重放旧答案，P0-3）。
    """
    if not settings.semantic_cache_enabled:
        return None
    stmt = (
        select(SemanticCache)
        .order_by(SemanticCache.updated_at.desc())
        .limit(settings.semantic_cache_pool)
    )
    # 候选池按检索作用域过滤（SQL 层，避免别的库/文档/风格/用户的缓存占满候选池）
    if user_id is None:
        stmt = stmt.where(SemanticCache.user_id.is_(None))
    else:
        stmt = stmt.where(SemanticCache.user_id == user_id)
    if kb_id is None:
        stmt = stmt.where(SemanticCache.kb_id.is_(None))
    else:
        stmt = stmt.where(SemanticCache.kb_id == kb_id)
    if doc_scope is None:
        stmt = stmt.where(SemanticCache.doc_scope.is_(None))
    else:
        stmt = stmt.where(SemanticCache.doc_scope == doc_scope)
    if style is None:
        stmt = stmt.where(SemanticCache.style.is_(None))
    else:
        stmt = stmt.where(SemanticCache.style == style)
    rows = (await db.execute(stmt)).scalars().all()
    rows = [r for r in rows if not _is_expired(r)]
    best: SemanticCache | None = None
    best_sim = 0.0
    for row in rows:
        try:
            vec = json.loads(row.query_vector_json)
            sim = _cosine(query_vector, vec)
        except Exception:
            continue
        if sim > best_sim:
            best = row
            best_sim = sim
    if best and best_sim >= settings.semantic_cache_threshold and _subjects_match(subject, best.subject):
        best.hit_count += 1
        best.updated_at = _now()
        await db.commit()
        logger.debug("语义缓存命中 sim=%.3f subject=%s scope=%s/%s", best_sim, best.subject, best.kb_id, best.doc_scope)
        return best.answer, json.loads(best.citations_json)
    return None


async def store(
    db: AsyncSession,
    query_vector: list[float],
    subject: str | None,
    answer: str,
    citations: list[dict],
    kb_id: int | None = None,
    doc_scope: str | None = None,
    style: str | None = None,
    user_id: int | None = None,
) -> None:
    """存储缓存条目（含主题词 + 检索作用域 + 回答风格 + 用户）；容量超限按 (user,kb) 作用域淘汰最旧。

    P0-3：缓存按用户隔离；写入前顺带清理同作用域过期条目。
    """
    if not settings.semantic_cache_enabled or not answer:
        return
    # 容量按 (user, kb) 作用域统计，避免单一用户/库写满把其它缓存挤掉
    scope_filter = (
        SemanticCache.kb_id.is_(None) if kb_id is None else SemanticCache.kb_id == kb_id
    )
    if user_id is None:
        scope_filter = and_(scope_filter, SemanticCache.user_id.is_(None))
    else:
        scope_filter = and_(scope_filter, SemanticCache.user_id == user_id)
    # 过期清理：同作用域超 TTL 的旧条目不占容量、不参与命中
    if settings.semantic_cache_ttl_seconds > 0:
        expire_before = _now() - timedelta(seconds=settings.semantic_cache_ttl_seconds)
        await db.execute(
            delete(SemanticCache).where(scope_filter, SemanticCache.updated_at < expire_before)
        )
    total = (
        await db.scalar(select(func.count()).select_from(SemanticCache).where(scope_filter))
    ) or 0
    if total >= settings.semantic_cache_max_entries:
        # 淘汰该作用域内最旧的 20%
        kill = max(1, settings.semantic_cache_max_entries // 5)
        oldest = await db.execute(
            select(SemanticCache.id)
            .where(scope_filter)
            .order_by(SemanticCache.updated_at)
            .limit(kill)
        )
        ids = [r for r in oldest.scalars().all()]
        if ids:
            await db.execute(delete(SemanticCache).where(SemanticCache.id.in_(ids)))
    db.add(
        SemanticCache(
            query_vector_json=json.dumps(query_vector),
            subject=subject,
            kb_id=kb_id,
            doc_scope=doc_scope,
            style=style,
            user_id=user_id,
            answer=answer,
            citations_json=json.dumps(citations, ensure_ascii=False),
        )
    )
    await db.commit()
