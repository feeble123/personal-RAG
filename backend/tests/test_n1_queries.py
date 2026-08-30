"""单元 H-①：会话历史加载 N+1 消除回归测试。

背景：`GET /conversations/{id}/messages` 旧实现里，每条 assistant 消息单独
`select(Citation).where(message_id==m.id)` 查一次引用——30 条消息 = ~15 次额外 SQL。
修复后改用 `selectinload(Message.citations)` 一次带出，SQL 条数应**固定**（不随消息数增长）。

本测试用 SQLAlchemy 事件监听器统计 `list_messages` 触发的 SQL 条数：
- 造 10 条带引用的 assistant 消息
- 断言 SQL 条数远小于「1 + 消息数」（selectinload 生效的硬证据）
- 断言返回的引用仍按 rank 排序（结果语义不变）
"""
from __future__ import annotations

import pytest
from sqlalchemy import event

from app.db.models import Citation, Conversation, Message
from app.db.session import async_session_factory, engine


async def _seed_conv_with_citations(user_id: int, n_assistant: int) -> int:
    """造一个会话 + n 条带引用的 assistant 消息（每条 3 条引用，rank 乱序），返回 conv_id。"""
    async with async_session_factory() as db:
        conv = Conversation(user_id=user_id, title="N+1 测试")
        db.add(conv)
        await db.flush()

        for i in range(n_assistant):
            # user 消息 + assistant 消息交替
            db.add(Message(conversation_id=conv.id, role="user", content=f"问题{i}", is_complete=True))
            asst = Message(
                conversation_id=conv.id, role="assistant", content=f"答案{i}", is_complete=True
            )
            db.add(asst)
            await db.flush()
            # 3 条引用，rank 乱序（3,1,2）验证排序
            for rank, title in [(3, "a"), (1, "b"), (2, "c")]:
                db.add(
                    Citation(
                        message_id=asst.id,
                        source=f"src{i}-{title}",
                        snippet=f"snippet{i}-{title}",
                        rank=rank,
                    )
                )
        await db.commit()
        return conv.id


async def _current_user_id(client, user_headers) -> int:
    """通过 /auth/me 拿当前登录用户的真实 id。"""
    r = await client.get("/api/auth/me", headers=user_headers)
    assert r.status_code == 200
    return r.json()["id"]


@pytest.mark.asyncio
async def test_list_messages_no_n1(client, user_headers):
    """核心断言：SQL 条数不随消息数线性增长（selectinload 生效）。"""
    user_id = await _current_user_id(client, user_headers)
    conv_id = await _seed_conv_with_citations(user_id, n_assistant=10)

    # 用事件监听器统计一次 list_messages 的 SQL 条数
    counts = {"n": 0}

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        counts["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _on_exec)
    try:
        r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _on_exec)

    assert r.status_code == 200
    items = r.json()["items"]

    # 20 条消息（10 user + 10 assistant）
    assert len(items) == 20
    # SQL 条数应远小于消息数（旧 N+1 会是 ~10+ 条引用查询；selectinload 固定 2~3 条）
    # 用宽松上界：SQL 条数 < 8（覆盖 1 条 message 查询 + 1 条 citation 批量 + 若干 overhead）
    assert counts["n"] < 8, f"N+1 疑似未消除：SQL 条数 = {counts['n']}"


@pytest.mark.asyncio
async def test_list_messages_citations_sorted_by_rank(client, user_headers):
    """结果语义不变：引用按 rank 升序返回。"""
    user_id = await _current_user_id(client, user_headers)
    conv_id = await _seed_conv_with_citations(user_id, n_assistant=1)

    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    items = r.json()["items"]
    # 最后一条是 assistant 答案
    asst = [m for m in items if m["role"] == "assistant"][-1]
    ranks = [c["rank"] for c in asst["citations"]]
    assert ranks == [1, 2, 3], f"引用应按 rank 升序，实际 {ranks}"
