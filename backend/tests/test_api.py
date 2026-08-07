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
