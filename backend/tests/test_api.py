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


async def test_chat_greeting_without_bm25_hits(client, user_headers, sample_kb):
    """问候语（BM25 全 0 命中）不得触发除零崩溃：rag.retrieve max_s==0 时应跳过归一化。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "你好", "kb_id": kb_id},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    assert events[-1]["event"] == "done", f"不应报错: {events[-1]}"


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


# ================= 记忆库管理系统（M1 后端 API）=================
async def test_memories_admin_list_filter_and_names(client, admin_headers, sample_kb):
    """管理员列表补 username/kb_name；按 kb_id/status/关键词筛选。"""
    kb_id, _ = sample_kb
    r = await client.post(
        "/api/admin/memories",
        headers=admin_headers,
        json={"question": "记忆管理系统筛选测试", "answer": "测试答案", "kb_id": kb_id, "style": "standard"},
    )
    assert r.status_code == 201
    mid = r.json()["id"]

    r = await client.get("/api/admin/memories", headers=admin_headers)
    body = r.json()
    item = next((i for i in body["items"] if i["id"] == mid), None)
    assert item is not None
    assert item["username"] == "admin"
    assert item["kb_name"]  # join 已补出库名

    r = await client.get(f"/api/admin/memories?kb_id={kb_id}", headers=admin_headers)
    assert all(i["kb_id"] == kb_id for i in r.json()["items"])
    r = await client.get("/api/admin/memories?q=记忆管理系统", headers=admin_headers)
    assert r.json()["total"] >= 1
    r = await client.get("/api/admin/memories?status=good", headers=admin_headers)
    assert any(i["id"] == mid for i in r.json()["items"])


async def test_memories_403_for_regular_user(client, user_headers):
    r = await client.get("/api/admin/memories", headers=user_headers)
    assert r.status_code == 403


async def test_memory_delete_single_and_404(client, admin_headers):
    r = await client.post(
        "/api/admin/memories", headers=admin_headers, json={"question": "待删除记忆", "answer": "答案"},
    )
    assert r.status_code == 201
    mid = r.json()["id"]
    r = await client.delete(f"/api/admin/memories/{mid}", headers=admin_headers)
    assert r.status_code == 204
    r = await client.get("/api/admin/memories?q=待删除记忆", headers=admin_headers)
    assert r.json()["total"] == 0
    r = await client.delete(f"/api/admin/memories/{mid}", headers=admin_headers)
    assert r.status_code == 404


async def test_memory_batch_delete(client, admin_headers):
    ids = []
    for q in ("批量删除一", "批量删除二"):
        r = await client.post("/api/admin/memories", headers=admin_headers, json={"question": q, "answer": "答案"})
        ids.append(r.json()["id"])
    r = await client.request(
        "DELETE",
        "/api/admin/memories",
        headers={**admin_headers, "Content-Type": "application/json"},
        content=json.dumps({"ids": ids}),
    )
    assert r.status_code == 204
    r = await client.get("/api/admin/memories", headers=admin_headers)
    assert all(i["id"] not in ids for i in r.json()["items"])


async def test_clear_kb_memories(client, admin_headers, sample_kb):
    kb_id, _ = sample_kb
    await client.post(
        "/api/admin/memories", headers=admin_headers,
        json={"question": "清库测试", "answer": "答案", "kb_id": kb_id},
    )
    r = await client.get(f"/api/admin/memories?kb_id={kb_id}", headers=admin_headers)
    assert r.json()["total"] >= 1
    r = await client.delete(f"/api/admin/kbs/{kb_id}/memories", headers=admin_headers)
    assert r.status_code == 204
    r = await client.get(f"/api/admin/memories?kb_id={kb_id}", headers=admin_headers)
    assert r.json()["total"] == 0


async def test_memory_status_correction_triggers_rerank(client, admin_headers, user_headers, sample_kb):
    """管理员把 good 记忆纠正为 bad → 同题再问强制重检（不再记忆复用/缓存命中）。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    async def ask(q):
        ev = []
        async with client.stream(
            "POST", f"/api/conversations/{conv_id}/chat", headers=user_headers,
            json={"content": q, "kb_id": kb_id},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    ev.append(json.loads(line[5:]))
        return ev

    q = "记忆纠偏后应强制重检"
    e1 = await ask(q)
    mid1 = e1[-1]["data"]["message_id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages/{mid1}/feedback",
        headers=user_headers, json={"feedback": "up"},
    )
    e2 = await ask(q)
    assert e2[-1]["data"].get("from_memory") is True

    # 管理员查到此记忆并纠正为 bad
    r = await client.get("/api/admin/memories?q=记忆纠偏后应强制重检", headers=admin_headers)
    mem_id = r.json()["items"][0]["id"]
    r = await client.patch(f"/api/admin/memories/{mem_id}", headers=admin_headers, json={"status": "bad"})
    assert r.status_code == 200 and r.json()["status"] == "bad"

    # 再问 → 强制重检（无记忆复用、非缓存命中）
    e3 = await ask(q)
    done = e3[-1]["data"]
    assert done.get("from_memory") is not True
    assert done.get("cached") is not True


async def test_memory_manual_create_and_recall(client, admin_headers):
    """管理员手动录入 → 同题再问命中记忆秒回。"""
    q = "手动录入后应被记忆复用"
    r = await client.post(
        "/api/admin/memories", headers=admin_headers,
        json={"question": q, "answer": "这是管理员手动录入的答案", "style": "standard"},
    )
    assert r.status_code == 201

    r = await client.post("/api/conversations", headers=admin_headers, json={})
    conv_id = r.json()["id"]
    ev = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat", headers=admin_headers,
        json={"content": q, "style": "standard"},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                ev.append(json.loads(line[5:]))
    assert ev[-1]["data"].get("from_memory") is True


async def test_memory_export(client, admin_headers):
    await client.post(
        "/api/admin/memories", headers=admin_headers,
        json={"question": "导出验证专用问题", "answer": "导出专用答案"},
    )
    r = await client.get("/api/admin/memories/export?fmt=json", headers=admin_headers)
    assert r.status_code == 200
    assert "导出验证专用问题" in r.text
    r = await client.get("/api/admin/memories/export?fmt=csv", headers=admin_headers)
    assert r.status_code == 200
    assert "导出验证专用问题" in r.text


async def test_stats_has_qa_memory(client, admin_headers):
    r = await client.get("/api/admin/stats", headers=admin_headers)
    assert r.status_code == 200
    assert "qa_memory" in r.json()


# ================= 账号管理系统（后端扩展）=================
async def test_admin_create_user_and_validation(client, admin_headers):
    r = await client.post(
        "/api/admin/users", headers=admin_headers,
        json={"username": "created_user", "password": "pass123", "nickname": "创建的用户"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "user" and body["is_active"] is True
    assert "created_at" in body
    uid = body["id"]
    # 重名 → 409
    r = await client.post("/api/admin/users", headers=admin_headers, json={"username": "created_user", "password": "pass123"})
    assert r.status_code == 409
    # 非法角色 → 422
    r = await client.post("/api/admin/users", headers=admin_headers, json={"username": "bad_role", "password": "pass123", "role": "super"})
    assert r.status_code == 422
    await client.delete(f"/api/admin/users/{uid}", headers=admin_headers)


async def test_admin_user_list_search(client, admin_headers):
    for uname in ("search_alpha", "search_beta"):
        await client.post("/api/admin/users", headers=admin_headers, json={"username": uname, "password": "pass123"})
    r = await client.get("/api/admin/users?q=search_alpha", headers=admin_headers)
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["username"] == "search_alpha"
    assert body["items"][0]["created_at"]
    # 清理
    r = await client.get("/api/admin/users?q=search_", headers=admin_headers)
    for u in r.json()["items"]:
        await client.delete(f"/api/admin/users/{u['id']}", headers=admin_headers)


async def test_admin_reset_password(client, admin_headers):
    r = await client.post("/api/admin/users", headers=admin_headers, json={"username": "reset_me", "password": "oldpass123"})
    uid = r.json()["id"]
    r = await client.put(f"/api/admin/users/{uid}/password", headers=admin_headers, json={"new_password": "newpass456"})
    assert r.status_code == 200
    # 旧密码失效、新密码可登录
    r = await client.post("/api/auth/login", json={"username": "reset_me", "password": "oldpass123"})
    assert r.status_code == 401
    r = await client.post("/api/auth/login", json={"username": "reset_me", "password": "newpass456"})
    assert r.status_code == 200
    # 重置不存在的用户 → 404
    r = await client.put("/api/admin/users/999999/password", headers=admin_headers, json={"new_password": "newpass456"})
    assert r.status_code == 404
    await client.delete(f"/api/admin/users/{uid}", headers=admin_headers)


async def test_admin_patch_role_and_active(client, admin_headers):
    r = await client.post("/api/admin/users", headers=admin_headers, json={"username": "patch_me", "password": "pass123"})
    uid = r.json()["id"]
    r = await client.patch(f"/api/admin/users/{uid}", headers=admin_headers, json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # 禁用 → 登录被拒
    await client.patch(f"/api/admin/users/{uid}", headers=admin_headers, json={"is_active": False})
    r = await client.post("/api/auth/login", json={"username": "patch_me", "password": "pass123"})
    assert r.status_code == 403
    await client.patch(f"/api/admin/users/{uid}", headers=admin_headers, json={"is_active": True})
    await client.delete(f"/api/admin/users/{uid}", headers=admin_headers)


async def test_admin_delete_and_self_guards(client, admin_headers):
    me = (await client.get("/api/auth/me", headers=admin_headers)).json()
    # 不能删除自己 / 改自己角色
    r = await client.delete(f"/api/admin/users/{me['id']}", headers=admin_headers)
    assert r.status_code == 400
    r = await client.patch(f"/api/admin/users/{me['id']}", headers=admin_headers, json={"role": "user"})
    assert r.status_code == 400
    # 删除不存在 → 404
    r = await client.delete("/api/admin/users/999999", headers=admin_headers)
    assert r.status_code == 404


async def test_users_403_for_regular_user(client, user_headers):
    r = await client.get("/api/admin/users", headers=user_headers)
    assert r.status_code == 403
    r = await client.post("/api/admin/users", headers=user_headers, json={"username": "x", "password": "pass123"})
    assert r.status_code == 403


async def test_admin_delete_user_cascades_memory(client, admin_headers, sample_kb):
    """删除用户 → 级联清理其会话/消息，并显式清理其沉淀的问答记忆（QaMemory 无 FK）。"""
    from sqlalchemy import func, select

    from app.db.models import QaMemory
    from app.db.session import async_session_factory

    kb_id, _ = sample_kb
    r = await client.post("/api/auth/register", json={"username": "cascade_u", "password": "pass123"})
    uh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/api/conversations", headers=uh, json={})
    conv_id = r.json()["id"]
    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=uh, json={"content": "级联清理验证", "kb_id": kb_id},
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    mid = events[-1]["data"]["message_id"]
    await client.post(f"/api/conversations/{conv_id}/messages/{mid}/feedback", headers=uh, json={"feedback": "up"})

    uid = (await client.get("/api/auth/me", headers=uh)).json()["id"]
    async with async_session_factory() as db:
        before = (await db.scalar(select(func.count()).select_from(QaMemory).where(QaMemory.user_id == uid))) or 0
        assert before >= 1  # 确认用户确实沉淀了记忆

    r = await client.delete(f"/api/admin/users/{uid}", headers=admin_headers)
    assert r.status_code == 204
    async with async_session_factory() as db:
        after = (await db.scalar(select(func.count()).select_from(QaMemory).where(QaMemory.user_id == uid))) or 0
        assert after == 0  # 记忆已随账号级联清理


# ================= 统计 =================
async def test_stats(client, admin_headers):
    r = await client.get("/api/admin/stats", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    for key in ("users", "conversations", "messages", "knowledge_bases", "chunks"):
        assert key in body
    # U3：证据质量分布字段存在且分档合计等于总数
    ev = body["evidence"]
    assert ev["total"] == ev["sufficient"] + ev["partial"] + ev["weak"] + ev["none"]
    # 层2：答案完备率统计字段
    av = body["answer_verify"]
    assert av["verified"] == av["complete"] + av["incomplete"]


# ================= 层2：答案完备性校验 + LLM优化（opt-in 按钮） =================
def test_verify_parse_json():
    from app.services import verify

    assert verify._parse_json('{"enumeration": true, "complete": false}') == {
        "enumeration": True,
        "complete": False,
    }
    assert verify._parse_json('前缀 {"ok": true} 后缀')["ok"] is True
    assert verify._parse_json("没有 json") == {}
    # 长回答首尾压缩：截断发生在尾部，校验器必须能看到结尾
    assert verify._head_tail("a" * 100) == "a" * 100
    ht = verify._head_tail("头" + "中" * 5000 + "尾", head=10, tail=2)
    assert ht.startswith("头")
    assert ht.endswith("尾")
    assert "省略" in ht


async def _ask(client, conv_id, q, kb_id, headers):
    """发一条普通问答，返回 SSE 事件列表。"""
    ev = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=headers, json={"content": q, "kb_id": kb_id},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                ev.append(json.loads(line[5:]))
    return ev


async def test_auto_verify_disabled_by_default(client, user_headers, sample_kb):
    """完备性校验默认关闭（opt-in）：普通问答不触发 reset，answer_complete 未打标。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    ev = await _ask(client, conv_id, "明渠均匀流的形成条件有哪些", kb_id, user_headers)
    assert "reset" not in [e["event"] for e in ev]
    assert ev[-1]["data"].get("answer_complete") is None
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    asst = [m for m in r.json()["items"] if m["role"] == "assistant"][-1]
    assert asst["answer_complete"] is None
    assert asst["optimized"] is False


async def _chat_then_optimize(client, conv_id, q, kb_id, headers):
    """发一条普通问答，再对该回答触发 /optimize，返回 (原回答id, 优化事件列表)。"""
    await _ask(client, conv_id, q, kb_id, headers)
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=headers)
    asst = [m for m in r.json()["items"] if m["role"] == "assistant"][-1]
    ev = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/messages/{asst['id']}/optimize",
        headers=headers, json={},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                ev.append(json.loads(line[5:]))
    return asst["id"], ev


async def test_optimize_generates_new_message(client, user_headers, sample_kb):
    """点「LLM优化」→ 整文档扩展证据重生成 → 落库新消息（optimized=True，原回答保留）。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    orig_id, ev = await _chat_then_optimize(client, conv_id, "明渠均匀流的形成条件有哪些", kb_id, user_headers)
    kinds = [e["event"] for e in ev]
    assert kinds[0] == "citations"
    assert "delta" in kinds
    assert kinds[-1] == "done"
    assert ev[-1]["data"].get("optimized") is True
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    assts = [m for m in r.json()["items"] if m["role"] == "assistant"]
    assert len(assts) == 2, "优化应新增一条消息（原回答保留可对比）"
    assert assts[0]["id"] == orig_id and assts[0]["optimized"] is False
    new = assts[1]
    assert new["optimized"] is True and new["is_complete"] and new["citations"]


async def test_optimize_resets_when_incomplete(client, user_headers, sample_kb, monkeypatch):
    """校验判定「枚举且不完整」→ 优化流程触发 reset 重生成；最终仍不全则 answer_complete=False。"""
    from app.services import verify as verify_mod

    async def fake_verify(query, answer, cites):
        return verify_mod.CompletenessVerdict(enumeration=True, complete=False, note="缺章节")

    monkeypatch.setattr(verify_mod, "verify_completeness", fake_verify)
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    _, ev = await _chat_then_optimize(client, conv_id, "明渠均匀流的形成条件有哪些", kb_id, user_headers)
    assert "reset" in [e["event"] for e in ev], "校验不完整应触发 reset 重生成"
    assert ev[-1]["data"].get("answer_complete") is False
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    new = [m for m in r.json()["items"] if m["role"] == "assistant"][-1]
    assert new["answer_complete"] is False and new["optimized"] is True


async def test_optimize_guards(client, user_headers, sample_kb):
    """optimize 端点守卫：消息不存在 / 不是助手消息 → 404。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/999999/optimize", headers=user_headers, json={}
    )
    assert r.status_code == 404
    # 用户消息不可优化
    await _ask(client, conv_id, "明渠均匀流的形成条件", kb_id, user_headers)
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    user_msg = r.json()["items"][0]
    r = await client.post(
        f"/api/conversations/{conv_id}/messages/{user_msg['id']}/optimize", headers=user_headers, json={}
    )
    assert r.status_code == 404


# ================= 证据等级（U3：四级判级 + 不足拒答）=================
def test_judge_evidence_level():
    """四级判级边界：充足 / 部分 / 较弱 / 不足。"""
    from app.services.rag import judge_evidence_level

    assert judge_evidence_level([]) == "none"
    assert judge_evidence_level([0.9]) == "sufficient"  # top1 高分
    assert judge_evidence_level([0.7, 0.65]) == "sufficient"  # 多块强相关交叉印证
    assert judge_evidence_level([0.55, 0.1]) == "partial"
    assert judge_evidence_level([0.4]) == "weak"
    assert judge_evidence_level([0.2, 0.1]) == "none"


async def test_chat_evidence_level_persisted(client, user_headers, sample_kb):
    """正常问答：证据判级随 done 事件返回，并落库到历史消息。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "明渠均匀流的形成条件", "kb_id": kb_id},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    done = events[-1]["data"]
    assert done["evidence_level"] in ("sufficient", "partial", "weak", "none")
    # 历史消息还原判级
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    asst = [m for m in r.json()["items"] if m["role"] == "assistant"][-1]
    assert asst["evidence_level"] == done["evidence_level"]
    assert asst["evidence_top_score"] is None or isinstance(asst["evidence_top_score"], float)


async def test_chat_evidence_none_real_time_refuses(client, user_headers, sample_kb, monkeypatch):
    """证据不足 + 实时/外部类问题（现在几点了）→ 直接拒答、引用为空、落库 none 判级。"""
    from app.services import rag as rag_mod

    monkeypatch.setattr(rag_mod, "judge_evidence_level", lambda scores: "none")
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "现在几点了", "kb_id": kb_id},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    assert events[0]["event"] == "citations" and events[0]["data"] == []  # 无引用
    refusal = "".join(e.get("data", "") for e in events if e["event"] == "delta")
    assert "实时" in refusal
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["evidence_level"] == "none"
    # 历史中该回答标记为证据不足
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=user_headers)
    asst = [m for m in r.json()["items"] if m["role"] == "assistant"][-1]
    assert asst["evidence_level"] == "none"


async def test_chat_evidence_none_greeting_allowed(client, user_headers, sample_kb, monkeypatch):
    """证据不足 + 问候/闲聊类 → 动态放行（不拒答），由 LLM 正常回答并落库 none 判级。"""
    from app.services import rag as rag_mod

    monkeypatch.setattr(rag_mod, "judge_evidence_level", lambda scores: "none")
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    events = []
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "你好", "kb_id": kb_id},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    content = "".join(e.get("data", "") for e in events if e["event"] == "delta")
    assert "实时" not in content  # 未走拒答
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["evidence_level"] == "none"


async def test_admin_stats_evidence_distribution(client, admin_headers, user_headers, sample_kb, monkeypatch):
    """管理端统计的证据质量分布：正常问答 + 实时类拒答各计一档，合计一致。"""
    from app.services import rag as rag_mod

    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]
    async with client.stream(
        "POST", f"/api/conversations/{conv_id}/chat",
        headers=user_headers, json={"content": "明渠均匀流", "kb_id": kb_id},
    ) as resp:
        async for _ in resp.aiter_lines():
            pass

    monkeypatch.setattr(rag_mod, "judge_evidence_level", lambda scores: "none")
    r2 = await client.post("/api/conversations", headers=user_headers, json={})
    conv2 = r2.json()["id"]
    async with client.stream(
        "POST", f"/api/conversations/{conv2}/chat",
        headers=user_headers, json={"content": "现在几点了", "kb_id": kb_id},
    ) as resp:
        async for _ in resp.aiter_lines():
            pass

    r = await client.get("/api/admin/stats", headers=admin_headers)
    assert r.status_code == 200
    ev = r.json()["evidence"]
    assert ev["total"] >= 2
    assert ev["none"] >= 1
    assert ev["total"] == ev["sufficient"] + ev["partial"] + ev["weak"] + ev["none"]


# ================= 追问改写（BUG-追问引用错位）=================
def test_completeness_helpers():
    """枚举/概述检测（跨库通用）+ 章节直接归属（排除嵌套附件）。"""
    from app.services.rag import _ENUMERATION_RE, _under_component

    # 直接归属：不含嵌套附件
    sec = "7 附则 / 二、成员单位职责 / 附件4 / 市应急管理专家防汛抗旱组成员名单"
    assert _under_component(sec, "市应急管理专家防汛抗旱组成员名单") is True
    nested = sec + " / 附件5 / 市应急管理专家应急救援组成员名单"
    assert _under_component(nested, "市应急管理专家防汛抗旱组成员名单") is False  # 排除嵌套附件5
    # 枚举/概述类（跨库通用问句）
    for q in (
        "请给我一份完整的专家信息",
        "所有专家有哪些",
        "还有其他单位吗",
        "我要完整的版本",
        "请问这份资料中包含哪些方案",
        "请列列出该文件中所有的方案名称",
        "这份资料里有哪几个清单",
    ):
        assert _ENUMERATION_RE.search(q), q
    # 单点取值 / 聚焦查询 → 不触发枚举扩展
    assert not _ENUMERATION_RE.search("明渠均匀流的形成条件是什么")
    assert not _ENUMERATION_RE.search("工作脚手架专项施工方案是什么时候进行专家论证的")
    assert not _ENUMERATION_RE.search("高支模专项施工方案的报审时间")


async def test_completeness_expansion_covers_full_list(client, user_headers, admin_headers):
    """完整性查询（完整的专家名单）→ 检索扩展覆盖列表章节全部切片（> top_k），不遗漏。"""
    import asyncio

    from app.core.config import settings
    from app.db.session import async_session_factory
    from app.services import rag

    name = f"完整性测试库"
    r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": name})
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]
    # 生成足够长（> top_k 块）的「附件1 专家名单」章节
    entries = "\n\n".join(
        f"第{i}位专家 专家名{i} 正高级工程师 从事水利水电工程设计与咨询 主要研究方向为水工结构与岩土工程 "
        f"工作单位某设计院 联系方式 1380000{i:04d} 从业二十余年 参与多个大型水利工程与防汛抗旱项目 "
        f"擅长流域防洪与水库调度 曾获省部级科技进步奖 具备注册土木工程师执业资格"
        for i in range(1, 40)
    )
    md = f"# 测试规范\n\n## 附件1 专家名单\n\n{entries}\n"
    r = await client.post(
        f"/api/admin/kbs/{kb_id}/documents/upload",
        headers=admin_headers,
        files={"file": ("list.md", md.encode("utf-8"), "text/markdown")},
    )
    doc_id = r.json()["id"]
    for _ in range(40):
        r = await client.get(f"/api/admin/kbs/{kb_id}/documents", headers=admin_headers)
        status = r.json()["items"][0]["status"]
        if status in ("ready", "failed"):
            break
        await asyncio.sleep(0.2)
    assert status == "ready", f"入库未完成: {status}"

    async with async_session_factory() as db:
        cites = await rag.retrieve(db, "请给我一份完整的专家名单", kb_id=kb_id, top_k=settings.top_k_final)
    assert len(cites) > settings.top_k_final, f"完整性查询应扩展超过 top_k({settings.top_k_final}): 实际 {len(cites)}"
    for c in cites:
        assert "名单" in (c.section or ""), f"引用应来自名单章节: {c.section}"

    await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)


def test_followup_rewrite():
    """追问改写：短/指代性追问合并上一轮问题；独立问题与纯问候不改写。"""
    from app.services.query_rewrite import needs_followup_rewrite, rewrite_followup_query

    prev = "重庆市水利电力勘测设计研究院的专家有哪些呢？"
    # 追问 → 需改写并合并上一轮问题
    assert needs_followup_rewrite("可以以表格的形式来呈现吗") is True
    assert rewrite_followup_query("可以以表格的形式来呈现吗", prev).startswith(prev.rstrip("？"))
    assert needs_followup_rewrite("帮我总结一下") is True
    assert needs_followup_rewrite("还有呢") is True
    # 独立问题 → 不改写
    assert needs_followup_rewrite("水库汛期调度运用计划包含哪些内容？") is False
    assert rewrite_followup_query("水库汛期调度运用计划包含哪些内容？", prev) == "水库汛期调度运用计划包含哪些内容？"
    # 纯问候 → 不改写
    assert needs_followup_rewrite("你好") is False
    assert needs_followup_rewrite("谢谢") is False


async def test_followup_question_reretrieves_same_topic(client, user_headers, sample_kb):
    """追问（可以给我总结一下吗）改写后重新检索到同主题切片，而非抄历史/检索错位。"""
    kb_id, _ = sample_kb
    r = await client.post("/api/conversations", headers=user_headers, json={})
    conv_id = r.json()["id"]

    async def ask(q):
        ev = []
        async with client.stream(
            "POST", f"/api/conversations/{conv_id}/chat",
            headers=user_headers, json={"content": q, "kb_id": kb_id},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    ev.append(json.loads(line[5:]))
        return ev

    e1 = await ask("明渠均匀流的形成条件包括哪些")
    src1 = {c["source"] for c in e1[0]["data"]}
    assert src1, "第一轮应有引用"

    e2 = await ask("可以给我总结一下吗")
    assert e2[0]["event"] == "citations" and e2[-1]["event"] == "done"
    src2 = {c["source"] for c in e2[0]["data"]}
    assert src2, "追问应重新检索到引用"
    # 改写后应命中同一份文档（同主题切片），而不是检索到无关来源
    assert src2 & src1, f"追问未命中同主题切片: {src2} vs {src1}"


def test_is_real_time_query():
    """意图分类：实时/外部信息类识别，领域/问候/概述/能力咨询一律放行。"""
    from app.services.intent import is_real_time_query

    # 实时/外部 → True（拒答）
    assert is_real_time_query("现在几点了") is True
    assert is_real_time_query("当前时间是多少") is True
    assert is_real_time_query("今天几号") is True
    assert is_real_time_query("今天天气怎么样") is True
    assert is_real_time_query("明天会下雨吗") is True
    assert is_real_time_query("未来24小时降雨情况") is True
    assert is_real_time_query("今天有什么新闻") is True
    assert is_real_time_query("最新消息") is True
    assert is_real_time_query("人民币兑美元汇率") is True

    # 领域/问候/概述/能力咨询 → False（放行）
    assert is_real_time_query("你好") is False
    assert is_real_time_query("你能做什么") is False
    assert is_real_time_query("介绍一下你的功能") is False
    assert is_real_time_query("明渠均匀流的形成条件是什么") is False
    assert is_real_time_query("水库汛期调度运用计划包含哪些内容") is False
    assert is_real_time_query("今天这个水库的水位是多少") is False
    assert is_real_time_query("《水闸设计规范》大概在讲什么") is False
    assert is_real_time_query("请问你能为我做什么") is False
