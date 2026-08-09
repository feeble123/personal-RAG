"""用户管理（仅管理员）：列表 / 创建 / 改角色 / 启停用 / 删除 / 重置密码 + 系统统计。"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select

from app.core.deps import AdminUser, DbSession
from app.core.exceptions import BizError
from app.core.security import hash_password
from app.db.models import Chunk, Conversation, Document, KnowledgeBase, Message, QaMemory, User
from app.modules.auth.schemas import USERNAME_RE, UserOut
from app.services import vector_store

router = APIRouter(prefix="/admin/users", tags=["users"])
stats_router = APIRouter(prefix="/admin", tags=["stats"])


class AdminUserOut(UserOut):
    """账号管理列表用：UserOut + 创建时间。"""

    created_at: datetime


class UserPatch(BaseModel):
    role: str | None = None  # admin / user
    is_active: bool | None = None


class UserCreateIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=64)
    nickname: str | None = Field(None, max_length=50)
    role: str = "user"

    @field_validator("username")
    @classmethod
    def check_username(cls, v: str) -> str:
        if not USERNAME_RE.match(v):
            raise ValueError("用户名须为 3-50 位字母/数字/下划线")
        return v

    @field_validator("role")
    @classmethod
    def check_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("角色须为 admin 或 user")
        return v


class PasswordResetIn(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=64)


class UserListOut(BaseModel):
    items: list[AdminUserOut]
    total: int


@router.get("", response_model=UserListOut)
async def list_users(
    db: DbSession,
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str | None = None,
) -> UserListOut:
    where = []
    if q:
        where.append(User.username.contains(q))
    total = (await db.scalar(select(func.count()).select_from(User).where(*where))) or 0
    rows = await db.execute(
        select(User).where(*where).order_by(User.created_at).offset((page - 1) * page_size).limit(page_size)
    )
    items = [AdminUserOut.model_validate(u) for u in rows.scalars().all()]
    return UserListOut(items=items, total=total)


@router.post("", response_model=AdminUserOut, status_code=201)
async def create_user(body: UserCreateIn, db: DbSession, _admin: AdminUser) -> AdminUserOut:
    """管理员直接创建账号（可指定角色），无需用户自助注册。"""
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise BizError("用户名已存在", 409, "USER_EXISTS")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        nickname=body.nickname or body.username,
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return AdminUserOut.model_validate(user)


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
    # QaMemory 无 FK 级联，需显式清理该用户沉淀的问答记忆
    await db.execute(delete(QaMemory).where(QaMemory.user_id == user_id))
    await db.delete(user)
    await db.commit()


@router.put("/{user_id}/password", response_model=UserOut)
async def reset_user_password(
    user_id: int, body: PasswordResetIn, db: DbSession, _admin: AdminUser
) -> UserOut:
    """管理员重置用户密码（不校验旧密码；用户下次用新密码登录）。"""
    user = await db.get(User, user_id)
    if not user:
        raise BizError("用户不存在", 404, "USER_NOT_FOUND")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


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

    from app.db.models import QaMemory

    # U3 检索证据质量分布：按 evidence_level 统计助手消息（论文「检索质量演化」数据来源）
    from sqlalchemy import func as sa_func, select as sa_select

    ev_rows = (
        await db.execute(
            sa_select(Message.evidence_level, sa_func.count())
            .where(Message.evidence_level.isnot(None))
            .group_by(Message.evidence_level)
        )
    ).all()
    evidence = {
        "total": sum(int(c) for _, c in ev_rows),
        "sufficient": 0,
        "partial": 0,
        "weak": 0,
        "none": 0,
    }
    for level, cnt in ev_rows:
        if level in evidence:
            evidence[level] = int(cnt)

    # 层2 完备率：经完备性校验的枚举类回答，第一遍即完整 / 触发补全重生成
    ac_rows = (
        await db.execute(
            select(Message.answer_complete, func.count())
            .where(Message.answer_complete.isnot(None))
            .group_by(Message.answer_complete)
        )
    ).all()
    answer_verify = {"verified": 0, "complete": 0, "incomplete": 0}
    for val, cnt in ac_rows:
        answer_verify["verified"] += int(cnt)
        if val:
            answer_verify["complete"] += int(cnt)
        else:
            answer_verify["incomplete"] += int(cnt)

    return {
        "users": await count(User),
        "conversations": await count(Conversation),
        "messages": await count(Message),
        "knowledge_bases": len(kb_rows),
        "documents": await count(Document),
        "chunks": await count(Chunk),
        "qa_memory": await count(QaMemory),  # 问答记忆库沉淀数
        "evidence": evidence,  # U3：检索证据质量分布
        "answer_verify": answer_verify,  # 层2：答案完备率
        "vectors_in_chroma": vector_count,
        "bm25_indexed_kbs": len(bm25.all_kb_ids()),
        "per_kb": [{"name": n, "chunk_count": c} for _, n, c in kb_rows],
    }
