"""端到端 API 测试（离线 FAKE LLM/Embedding）。"""
from __future__ import annotations

import json

import pytest


# ================= 认证 =================
async def test_register_login_me_change_password(client):
    # 注册
    r = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "pass123", "nickname": "爱丽丝"},
    )
    assert r.status_code == 201
    token = r.json()["access_token"]
    assert r.json()["user"]["role"] == "user"
    H = {"Authorization": f"Bearer {token}"}

    # me
    r = await client.get("/api/auth/me", headers=H)
    assert r.status_code == 200 and r.json()["username"] == "alice"

    # 改密
    r = await client.put(
        "/api/auth/password",
        headers=H,
        json={"old_password": "pass123", "new_password": "newpass456"},
    )
    assert r.status_code == 200
    # 旧密码失效
    r = await client.post("/api/auth/login", json={"username": "alice", "password": "pass123"})
    assert r.status_code == 401
    # 新密码有效
    r = await client.post("/api/auth/login", json={"username": "alice", "password": "newpass456"})
    assert r.status_code == 200


async def test_auth_guards(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401  # 未登录


async def test_duplicate_username(client):
    await client.post("/api/auth/register", json={"username": "dup", "password": "pass123"})
    r = await client.post("/api/auth/register", json={"username": "dup", "password": "pass123"})
    assert r.status_code == 409


# ================= 权限 =================
async def test_user_cannot_access_admin(client, user_headers):
    r = await client.get("/api/admin/kbs", headers=user_headers)
    assert r.status_code == 403


# ================= 知识库 + 入库 =================
async def test_kb_crud_and_ingestion(client, admin_headers):
    r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": "水力学库"})
    assert r.status_code == 201
    kb_id = r.json()["id"]

    r = await client.get("/api/admin/kbs", headers=admin_headers)
    assert any(k["id"] == kb_id for k in r.json())

    # 重名
    r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": "水力学库"})
    assert r.status_code == 409

    # 改名
    r = await client.patch(f"/api/admin/kbs/{kb_id}", headers=admin_headers, json={"name": "水力学新库"})
    assert r.json()["name"] == "水力学新库"

    # 删除
    await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)
    r = await client.get("/api/admin/kbs", headers=admin_headers)
    assert all(k["id"] != kb_id for k in r.json())


async def test_upload_ingestion_and_search(client, admin_headers, sample_kb):
    kb_id, doc_id = sample_kb
    # 文档列表
    r = await client.get(f"/api/admin/kbs/{kb_id}/documents", headers=admin_headers)
    assert r.json()["items"][0]["status"] == "ready"
    assert r.json()["items"][0]["chunk_count"] >= 1

    # 检索预览
    r = await client.get(
        "/api/admin/search",
        headers=admin_headers,
        params={"q": "明渠均匀流", "kb_id": kb_id},
    )
    assert r.status_code == 200
    assert len(r.json()["hits"]) >= 1
    assert r.json()["hits"][0]["source"] == "demo.md"

    # 删除文档
    r = await client.delete(f"/api/admin/documents/{doc_id}", headers=admin_headers)
    assert r.status_code == 204


async def test_upload_rejects_bad_extension(client, admin_headers, sample_kb):
    kb_id, _ = sample_kb
    r = await client.post(
        f"/api/admin/kbs/{kb_id}/documents/upload",
        headers=admin_headers,
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 400


# ================= 会话 + 问答 =================
async def test_conversation_isolation(client, user_headers, sample_kb):
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    # 另一个用户访问 → 404
    r2 = await client.post("/api/auth/register", json={"username": "other", "password": "pass123"})
    other_headers = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    r = await client.get(f"/api/conversations/{conv_id}", headers=other_headers)
    assert r.status_code == 404


async def test_chat_sse_and_history(client, user_headers, sample_kb):
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    events = []
    async with client.stream(
        "POST",
        f"/api/conversations/{conv_id}/chat",
        headers=user_headers,
        json={"content": "明渠均匀流的形成条件", "kb_id": kb_id},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    kinds = [e["event"] for e in events]
    assert kinds[0] == "citations"
    assert "delta" in kinds
    assert kinds[-1] == "done"
    assert events[0]["data"]  # 有引用

    # 标题自动生成
    r = await client.get("/api/conversations", headers=user_headers)
    assert r.json()["items"][0]["title"].startswith("明渠均匀流")

    # 历史消息 + 引用还原
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    msgs = r.json()["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    asst = msgs[-1]
    assert asst["citations"] and asst["is_complete"]


async def test_semantic_cache(client, user_headers, sample_kb):
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    async def ask(q):
        ev = []
        async with client.stream(
            "POST", f"/api/conversations/{conv_id}/chat", headers=user_headers, json={"content": q, "kb_id": kb_id}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    ev.append(json.loads(line[5:]))
        return ev

    e1 = await ask("什么是明渠均匀流")
    e2 = await ask("什么是明渠均匀流")
    assert e1[-1]["data"].get("cached", False) is False
    assert e2[-1]["data"].get("cached", False) is True


# ================= 问答记忆库（AI native 自身长库）=================
async def test_memory_feedback_and_reuse(client, user_headers, sample_kb):
    """👍 沉淀正向记忆 → 同题再问命中记忆（from_memory=True 且带真实 message_id）。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    async def ask(q):
        ev = []
        async with client.stream(
            "POST", f"/api/conversations/{conv_id}/chat", headers=user_headers, json={"content": q, "kb_id": kb_id}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    ev.append(json.loads(line[5:]))
        return ev

    e1 = await ask("什么是明渠均匀流的形成条件")
    assert e1[-1]["event"] == "done"
    mid = e1[-1]["data"]["message_id"]
    assert mid is not None  # 正常生成必带真实 message_id

    # 👍 沉淀正向记忆
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{mid}/feedback",
        headers=user_headers, json={"feedback": "up"},
    )
    assert r.status_code == 200
    assert r.json()["feedback"] == "up"

    # 同题再问 → 命中记忆（先于语义缓存）
    e2 = await ask("什么是明渠均匀流的形成条件")
    done = e2[-1]["data"]
    assert done.get("from_memory") is True
    assert done.get("cached") is True
    assert done.get("message_id") is not None


async def test_memory_negative_forces_rerank(client, user_headers, sample_kb):
    """👎 沉淀负面记忆 → 同题再问强制重新检索（跳过记忆复用与语义缓存）。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    async def ask(q):
        ev = []
        async with client.stream(
            "POST", f"/api/conversations/{conv_id}/chat", headers=user_headers, json={"content": q, "kb_id": kb_id}
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    ev.append(json.loads(line[5:]))
        return ev

    e1 = await ask("什么是明渠均匀流")
    mid1 = e1[-1]["data"]["message_id"]
    # 👎 沉淀负面记忆
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{mid1}/feedback",
        headers=user_headers, json={"feedback": "down"},
    )
    assert r.status_code == 200
    assert r.json()["feedback"] == "down"

    # 同题再问 → 强制重新检索：不出现记忆复用，也不走语义缓存
    e2 = await ask("什么是明渠均匀流")
    done = e2[-1]["data"]
    assert done.get("from_memory") is not True
    assert done.get("cached") is not True
    assert done.get("message_id") is not None


async def test_feedback_cancel_and_guards(client, user_headers, sample_kb):
    """取消评价 null；越权/非 assistant 消息 404。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "明渠均匀流的条件", "kb_id": kb_id},
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    mid = events[-1]["data"]["message_id"]

    # 取消评价
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{mid}/feedback",
        headers=user_headers, json={"feedback": None},
    )
    assert r.status_code == 200
    assert r.json()["feedback"] is None

    # 非 assistant 消息（取 user 消息 id）→ 404
    msgs = (await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)).json()["items"]
    user_msg_id = msgs[0]["id"]
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{user_msg_id}/feedback",
        headers=user_headers, json={"feedback": "up"},
    )
    assert r.status_code == 404

    # 越权：他人会话的 message → 404
    r2 = await client.post("/api/auth/register", json={"username": "intruder", "password": "pass123"})
    other_headers = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{mid}/feedback",
        headers=other_headers, json={"feedback": "up"},
    )
    assert r.status_code == 404


async def test_kb_delete_cascades_memory(client, admin_headers, user_headers, sample_kb):
    """删除知识库 → 该库沉淀的问答记忆一并清理（级联），避免悬空记忆污染。"""
    from sqlalchemy import func, select

    from app.db.models import QaMemory
    from app.db.session import async_session_factory

    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "什么是明渠均匀流", "kb_id": kb_id},
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    mid = events[-1]["data"]["message_id"]

    # 👍 沉淀正向记忆（落到该 kb_id）
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{mid}/feedback",
        headers=user_headers, json={"feedback": "up"},
    )
    assert r.status_code == 200
    async with async_session_factory() as db:
        cnt = (await db.scalar(select(func.count()).select_from(QaMemory).where(QaMemory.kb_id == kb_id))) or 0
        assert cnt >= 1

    # 删除知识库 → 记忆级联清理
    r = await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)
    assert r.status_code == 204
    async with async_session_factory() as db:
        cnt = (await db.scalar(select(func.count()).select_from(QaMemory).where(QaMemory.kb_id == kb_id))) or 0
        assert cnt == 0


# ================= 统计 =================
async def test_stats(client, admin_headers):
    r = await client.get("/api/admin/stats", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    for key in ("users", "conversations", "messages", "knowledge_bases", "chunks"):
        assert key in body
