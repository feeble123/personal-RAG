"""三级角色分层 集成测试（单元 I 补充）：账号管理仅超管，库管只管内容。

覆盖：
1. 超管创建三级角色账号（superadmin/admin/user）均成功
2. 库管(admin)访问账号管理 → 403；访问知识库管理 → 200
3. 库管尝试创建账号/改角色 → 403（无法自我提权）
4. 普通用户访问知识库管理 → 403（AdminUser 对 user 仍拒绝）
"""
from __future__ import annotations

import pytest


async def _login(client, username: str, password: str) -> dict:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_superadmin_create_three_tier_roles(client, admin_headers):
    """超管可创建三种角色账号，且落库角色正确。"""
    for role in ("superadmin", "admin", "user"):
        r = await client.post(
            "/api/admin/users",
            headers=admin_headers,
            json={"username": f"role_{role}", "password": "pass123", "role": role},
        )
        assert r.status_code == 201, r.text
        assert r.json()["role"] == role
        await client.delete(f"/api/admin/users/{r.json()['id']}", headers=admin_headers)


async def test_admin_cannot_manage_users(client, admin_headers):
    """库管(admin)不能进账号管理（无法增删账号/改角色/重置密码）。"""
    # 超管创建一个库管账号
    r = await client.post(
        "/api/admin/users",
        headers=admin_headers,
        json={"username": "kb_admin1", "password": "pass123", "role": "admin"},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    kb_admin_headers = await _login(client, "kb_admin1", "pass123")

    # 账号管理接口全部 403
    assert (await client.get("/api/admin/users", headers=kb_admin_headers)).status_code == 403
    assert (
        await client.post(
            "/api/admin/users",
            headers=kb_admin_headers,
            json={"username": "evil_admin", "password": "pass123", "role": "superadmin"},
        )
    ).status_code == 403
    # 改角色（自我提权）→ 403
    assert (
        await client.patch(f"/api/admin/users/{uid}", headers=kb_admin_headers, json={"role": "superadmin"})
    ).status_code == 403
    # 重置密码 → 403
    assert (
        await client.put(
            f"/api/admin/users/{uid}/password", headers=kb_admin_headers, json={"new_password": "hack123"}
        )
    ).status_code == 403

    # 但库管能进知识库管理（内容管理，AdminUser 放宽）
    assert (await client.get("/api/admin/kbs", headers=kb_admin_headers)).status_code == 200

    # 清理
    await client.delete(f"/api/admin/users/{uid}", headers=admin_headers)


async def test_regular_user_cannot_manage_kbs(client, user_headers):
    """普通用户访问知识库管理 → 403（AdminUser 对 user 仍拒绝）。"""
    assert (await client.get("/api/admin/kbs", headers=user_headers)).status_code == 403
