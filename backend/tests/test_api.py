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


# ================= 统计 =================
async def test_stats(client, admin_headers):
    r = await client.get("/api/admin/stats", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    for key in ("users", "conversations", "messages", "knowledge_bases", "chunks"):
        assert key in body
