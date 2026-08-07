"""问答记忆库管理系统（仅管理员）：列表/筛选/导出/单删/批量删/按库清空/状态纠正/手动录入。

与「知识库管理系统」平级的管理模块。数据来自 qa_memory 表（AI native 自身长库沉淀），
列表经 outerjoin User/KnowledgeBase 补出 username/kb_name。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.deps import AdminUser, DbSession
from app.core.exceptions import BizError
from app.db.models import KnowledgeBase, QaMemory, User
from app.modules.memory.schemas import (
    MemoryBatchDelete,
    MemoryCreate,
    MemoryListOut,
    MemoryOut,
    MemoryStatusUpdate,
)
from app.services import memory, rag
from app.services.embedding import embed_query

router = APIRouter(prefix="/admin/memories", tags=["qa_memory"])
kb_memories_router = APIRouter(prefix="/admin/kbs", tags=["qa_memory"])


def _memory_config() -> memory.MemoryConfig:
    return memory.MemoryConfig(
        enabled=settings.memory_enabled,
        threshold=settings.memory_threshold,
        max_entries=settings.memory_max_entries,
        pool=settings.memory_pool,
        eviction_ratio=settings.memory_eviction_ratio,
    )


def _memories_where(
    kb_id: int | None,
    user_id: int | None,
    status: str | None,
    style: str | None,
    q: str | None,
) -> list[Any]:
    """多条件筛选（列表与导出共用）。"""
    where: list[Any] = []
    if kb_id is not None:
        where.append(QaMemory.kb_id == kb_id)
    if user_id is not None:
        where.append(QaMemory.user_id == user_id)
    if status in ("good", "bad"):
        where.append(QaMemory.status == status)
    if style:
        where.append(QaMemory.style == style)
    if q:
        where.append(QaMemory.question.contains(q))
    return where


async def _to_out(db, rows: list[QaMemory]) -> list[MemoryOut]:
    """补 username/kb_name 并组装 MemoryOut。"""
    u_map: dict[int, str] = {}
    user_ids = {r.user_id for r in rows}
    if user_ids:
        u_rows = (await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))).all()
        u_map = {uid: uname for uid, uname in u_rows}
    k_map: dict[int, str] = {}
    kb_ids = {r.kb_id for r in rows if r.kb_id is not None}
    if kb_ids:
        k_rows = (await db.execute(select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(kb_ids)))).all()
        k_map = {kid: kname for kid, kname in k_rows}
    return [
        MemoryOut(
            id=r.id,
            user_id=r.user_id,
            username=u_map.get(r.user_id),
            kb_id=r.kb_id,
            kb_name=k_map.get(r.kb_id) if r.kb_id is not None else None,
            doc_scope=r.doc_scope,
            style=r.style,
            status=r.status,
            question=r.question,
            subject=r.subject,
            answer=r.answer,
            citations=json.loads(r.citations_json),
            hit_count=r.hit_count,
            score=r.score,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.get("", response_model=MemoryListOut)
async def list_memories(
    db: DbSession,
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    kb_id: int | None = None,
    user_id: int | None = None,
    status: str | None = None,
    style: str | None = None,
    q: str | None = None,
) -> MemoryListOut:
    where = _memories_where(kb_id, user_id, status, style, q)
    total = (await db.scalar(select(func.count()).select_from(QaMemory).where(*where))) or 0
    rows = (
        await db.execute(
            select(QaMemory)
            .where(*where)
            .order_by(QaMemory.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return MemoryListOut(items=await _to_out(db, rows), total=total)


@router.get("/stats")
async def memory_stats(
    db: DbSession,
    _admin: AdminUser,
    kb_id: int | None = None,
    user_id: int | None = None,
    status: str | None = None,
    style: str | None = None,
    q: str | None = None,
) -> dict:
    """记忆统计（支持同列表筛选）：总数 / good / bad / 总命中次数，供前端统计卡片。"""
    where = _memories_where(kb_id, user_id, status, style, q)
    total = (await db.scalar(select(func.count()).select_from(QaMemory).where(*where))) or 0
    good = (
        await db.scalar(
            select(func.count()).select_from(QaMemory).where(*where, QaMemory.status == "good")
        )
    ) or 0
    total_hits = (await db.scalar(select(func.sum(QaMemory.hit_count)).where(*where))) or 0
    return {"total": total, "good": good, "bad": total - good, "total_hits": total_hits}


@router.get("/export")
async def export_memories(
    db: DbSession,
    _admin: AdminUser,
    fmt: str = "csv",
    kb_id: int | None = None,
    user_id: int | None = None,
    status: str | None = None,
    style: str | None = None,
    q: str | None = None,
) -> StreamingResponse:
    """导出（CSV/JSON，带当前筛选）；CSV 带 UTF-8 BOM 兼容 Excel 中文。"""
    where = _memories_where(kb_id, user_id, status, style, q)
    rows = (
        await db.execute(
            select(QaMemory).where(*where).order_by(QaMemory.updated_at.desc()).limit(10000)
        )
    ).scalars().all()
    outs = await _to_out(db, rows)  # 一次性补用户名/库名（避免 N+1）
    if fmt == "json":
        payload = json.dumps([o.model_dump(mode="json") for o in outs], ensure_ascii=False, indent=2)
        return Response(
            content="﻿" + payload,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="qa_memory.json"'},
        )
    # CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "user_id", "username", "kb_id", "kb_name", "status", "question", "subject", "answer", "hit_count", "score", "updated_at"])
    for o in outs:
        writer.writerow([
            o.id, o.user_id, o.username or "", o.kb_id or "", o.kb_name or "", o.status,
            o.question, o.subject or "", o.answer, o.hit_count,
            "" if o.score is None else round(o.score, 4),
            o.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        ])
    # UTF-8 BOM：Excel 打开中文不乱码
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="qa_memory.csv"'},
    )


@router.post("", status_code=201, response_model=MemoryOut)
async def create_memory(body: MemoryCreate, db: DbSession, _admin: AdminUser) -> MemoryOut:
    """手动录入记忆：算向量+主题词 → memory.remember（复用去重/容量/状态逻辑）。"""
    qvec = await embed_query(body.question)
    subject = rag.focus_rerank_query(body.question)
    await memory.remember(
        db, qvec, subject, body.question, body.answer, body.citations,
        user_id=_admin.id, kb_id=body.kb_id, style=body.style,
        status="good", config=_memory_config(),
    )
    row = await db.scalar(
        select(QaMemory)
        .where(QaMemory.user_id == _admin.id, QaMemory.question == body.question)
        .order_by(QaMemory.updated_at.desc())
        .limit(1)
    )
    if row is None:
        raise BizError("记忆写入失败（可能已停用记忆功能）", 500, "MEMORY_WRITE_FAILED")
    return (await _to_out(db, [row]))[0]


@router.patch("/{mem_id}", response_model=MemoryOut)
async def update_memory_status(
    mem_id: int, body: MemoryStatusUpdate, db: DbSession, _admin: AdminUser
) -> MemoryOut:
    """手动纠正状态：good↔bad（等效管理员代用户👍/👎，下次同题命中行为随之改变）。"""
    row = await db.get(QaMemory, mem_id)
    if row is None:
        raise BizError("记忆不存在", 404, "MEM_NOT_FOUND")
    row.status = body.status
    await db.commit()
    await db.refresh(row)
    return (await _to_out(db, [row]))[0]


@router.delete("/{mem_id}", status_code=204)
async def delete_memory(mem_id: int, db: DbSession, _admin: AdminUser) -> None:
    row = await db.get(QaMemory, mem_id)
    if row is None:
        raise BizError("记忆不存在", 404, "MEM_NOT_FOUND")
    await db.delete(row)
    await db.commit()


@router.delete("", status_code=204)
async def batch_delete_memories(body: MemoryBatchDelete, db: DbSession, _admin: AdminUser) -> None:
    await db.execute(delete(QaMemory).where(QaMemory.id.in_(body.ids)))
    await db.commit()


@kb_memories_router.delete("/{kb_id}/memories", status_code=204)
async def clear_kb_memories(kb_id: int, db: DbSession, _admin: AdminUser) -> None:
    """清空某知识库沉淀的全部记忆（保留知识库本身）。"""
    await db.execute(delete(QaMemory).where(QaMemory.kb_id == kb_id))
    await db.commit()
