"""单元 I：审计日志 + 日志密钥脱敏 回归测试。

覆盖：
1. 脱敏：redact() 抹掉 API key / Bearer / password / DB 口令 / JWT / sk- 密钥，普通文本不受影响。
2. 审计：管理员敏感操作（创建/删除账号、创建知识库、上传/删除文档）写入 audit_logs，
   /admin/audit-logs 可列表查询、按 action 过滤；非管理员访问 403。
"""
from __future__ import annotations

import pytest

from app.core.redact import redact


# ---------------- 脱敏 ----------------
def test_redact_api_key_and_bearer():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def key=sk-abcdef123456789"
    out = redact(text)
    assert "sk-abcdef123456789" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_password_and_db_url():
    text = "postgresql://rag_app:secretpass@localhost:5432/rag password=secretpass"
    out = redact(text)
    assert "secretpass" not in out
    assert "rag_app:***@localhost" in out or "rag_app:******@localhost" in out


def test_redact_known_secret_values():
    # settings.jwt_secret / admin_password 等已知值被字面量替换
    out = redact("jwt_secret=test-secret-for-unit-tests-not-production")
    assert "test-secret-for-unit-tests-not-production" not in out


def test_redact_leaves_normal_text():
    text = "水利工程 明渠均匀流 3.4 节 1979 年"
    assert redact(text) == text


# ---------------- 审计 ----------------
@pytest.mark.asyncio
async def test_audit_logged_and_listable(client, admin_headers, user_headers):
    # 1) 管理员创建一个账号 → 产生 user.create 审计
    r = await client.post(
        "/api/admin/users",
        headers=admin_headers,
        json={"username": "audit_target1", "password": "pass123", "role": "user"},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    # 2) 删除该账号 → 产生 user.delete 审计
    r = await client.delete(f"/api/admin/users/{uid}", headers=admin_headers)
    assert r.status_code == 204

    # 3) 列表查询审计日志（应包含 user.create / user.delete）
    r = await client.get("/api/admin/audit-logs", headers=admin_headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    actions = {i["action"] for i in items}
    assert "user.create" in actions
    assert "user.delete" in actions

    # 4) 按 action 过滤
    r = await client.get("/api/admin/audit-logs", headers=admin_headers, params={"action": "user.create"})
    assert r.status_code == 200
    assert all(i["action"] == "user.create" for i in r.json()["items"])


@pytest.mark.asyncio
async def test_audit_requires_admin(client, user_headers):
    r = await client.get("/api/admin/audit-logs", headers=user_headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_kb_and_document_audit(client, admin_headers):
    # 创建知识库 → kb.create
    r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": "审计测试库"})
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]

    # 上传文档 → document.upload
    r = await client.post(
        f"/api/admin/kbs/{kb_id}/documents/upload",
        headers=admin_headers,
        files={"file": ("a.md", "# 标题\n内容".encode("utf-8"), "text/markdown")},
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]

    r = await client.get("/api/admin/audit-logs", headers=admin_headers, params={"q": "审计测试库"})
    actions = {i["action"] for i in r.json()["items"]}
    assert "kb.create" in actions

    # 删除文档 → document.delete
    r = await client.delete(f"/api/admin/documents/{doc_id}", headers=admin_headers)
    assert r.status_code == 204

    # 删除知识库 → kb.delete
    r = await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)
    assert r.status_code == 204

    r = await client.get("/api/admin/audit-logs", headers=admin_headers)
    actions = {i["action"] for i in r.json()["items"]}
    assert "document.upload" in actions
    assert "document.delete" in actions
    assert "kb.delete" in actions
