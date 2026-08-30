"""会话与消息路由：CRUD + 游标分页（懒加载历史）。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import BizError
from app.db.models import Conversation, Message
from app.modules.conversations.schemas import (
    ConversationCreate,
    ConversationListOut,
    ConversationOut,
    ConversationUpdate,
    MessageListOut,
    MessageOut,
)
from app.schemas import CitationOut

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_owned_conversation(db: DbSession, conv_id: int, user_id: int) -> Conversation:
    """校验会话归属；不存在或不属于该用户一律 404（不泄露存在性）。"""
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user_id:
        raise BizError("会话不存在", 404, "CONV_NOT_FOUND")
    return conv


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    db: DbSession,
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> ConversationListOut:
    total = (
        await db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user.id)
        )
    ) or 0
    rows = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.last_message_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [ConversationOut.model_validate(c) for c in rows.scalars().all()]
    return ConversationListOut(items=items, total=total)


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    db: DbSession,
    user: CurrentUser,
) -> Conversation:
    conv = Conversation(user_id=user.id, title=body.title or "新会话")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("/{conv_id}", response_model=ConversationOut)
async def get_conversation(conv_id: int, db: DbSession, user: CurrentUser) -> Conversation:
    return await get_owned_conversation(db, conv_id, user.id)


@router.patch("/{conv_id}", response_model=ConversationOut)
async def rename_conversation(
    conv_id: int,
    body: ConversationUpdate,
    db: DbSession,
    user: CurrentUser,
) -> Conversation:
    conv = await get_owned_conversation(db, conv_id, user.id)
    conv.title = body.title
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/{conv_id}", status_code=204)
async def delete_conversation(conv_id: int, db: DbSession, user: CurrentUser) -> None:
    conv = await get_owned_conversation(db, conv_id, user.id)
    await db.delete(conv)
    await db.commit()


@router.get("/{conv_id}/messages", response_model=MessageListOut)
async def list_messages(
    conv_id: int,
    db: DbSession,
    user: CurrentUser,
    cursor: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
) -> MessageListOut:
    """游标分页：返回 cursor 之前（更早）的消息，向前翻加载历史。"""
    await get_owned_conversation(db, conv_id, user.id)

    q = select(Message).where(Message.conversation_id == conv_id)
    if cursor:
        q = q.where(Message.id < cursor)
    q = q.order_by(Message.id.desc()).limit(limit + 1)
    # H-①：selectinload 一次性带出本批所有消息的引用，消除「每条消息单独查引用」的 N+1。
    # Message.citations 关系本就 lazy="selectin"，但列表接口需按 rank 排序展示，
    # selectinload 默认按主键序加载，故在 Python 侧按 rank 再排一次。
    q = q.options(selectinload(Message.citations))
    rows = list((await db.execute(q)).scalars().all())

    has_more = len(rows) > limit
    rows = rows[:limit]
    # 倒序展示：最新在最后
    rows.reverse()

    items: list[MessageOut] = []
    for m in rows:
        out = MessageOut.model_validate(m)
        if m.role == "assistant":
            cites = sorted(m.citations, key=lambda c: c.rank if c.rank is not None else 0)
            out.citations = [CitationOut.model_validate(c) for c in cites]
        items.append(out)
    return MessageListOut(items=items, has_more=has_more)
