"""用户管理（仅管理员）：列表 / 改角色 / 启停用 / 删除 + 系统统计。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.deps import AdminUser, DbSession
from app.core.exceptions import BizError
from app.db.models import Chunk, Conversation, Document, KnowledgeBase, Message, User
from app.modules.auth.schemas import UserOut
from app.services import vector_store

router = APIRouter(prefix="/admin/users", tags=["users"])
stats_router = APIRouter(prefix="/admin", tags=["stats"])


class UserPatch(BaseModel):
    role: str | None = None  # admin / user
    is_active: bool | None = None


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int


@router.get("", response_model=UserListOut)
async def list_users(
    db: DbSession,
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> UserListOut:
    total = (await db.scalar(select(func.count()).select_from(User))) or 0
    rows = await db.execute(
        select(User).order_by(User.created_at).offset((page - 1) * page_size).limit(page_size)
    )
    items = [UserOut.model_validate(u) for u in rows.scalars().all()]
    return UserListOut(items=items, total=total)


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: int,
    body: UserPatch,
    db: DbSession,
    admin: AdminUser,
) -> UserOut:
    if user_id == admin.id:
        raise BizError("不能修改自己的角色/状态", 400, "SELF_MODIFY")
    user = await db.get(User, user_id)
    if not user:
        raise BizError("用户不存在", 404, "USER_NOT_FOUND")
    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise BizError("非法角色", 400, "INVALID_ROLE")
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: int, db: DbSession, admin: AdminUser) -> None:
    if user_id == admin.id:
        raise BizError("不能删除自己", 400, "SELF_DELETE")
    user = await db.get(User, user_id)
    if not user:
        raise BizError("用户不存在", 404, "USER_NOT_FOUND")
    await db.delete(user)
    await db.commit()


# ---------------- 系统统计（答辩数据） ----------------
@stats_router.get("/stats")
async def system_stats(db: DbSession, _admin: AdminUser) -> dict:
    from app.services import bm25
    from app.services import semantic_cache  # noqa: F401

    async def count(model) -> int:
        return (await db.scalar(select(func.count()).select_from(model))) or 0

    kb_rows = (await db.execute(select(KnowledgeBase.id, KnowledgeBase.name, KnowledgeBase.chunk_count))).all()
    vector_count = 0
    try:
        vector_count = await asyncio.to_thread(vector_store.count)
    except Exception:
        pass

    return {
        "users": await count(User),
        "conversations": await count(Conversation),
        "messages": await count(Message),
        "knowledge_bases": len(kb_rows),
        "documents": await count(Document),
        "chunks": await count(Chunk),
        "vectors_in_chroma": vector_count,
        "bm25_indexed_kbs": len(bm25.all_kb_ids()),
        "per_kb": [{"name": n, "chunk_count": c} for _, n, c in kb_rows],
    }
