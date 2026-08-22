"""P0-1 单元2：认证加固（session 化 + refresh 轮换 + 改密/禁用立即失效）。

覆盖：
- 登录：access 短期 + refresh cookie 种入（HttpOnly）
- refresh 轮换：旧 refresh 作废、新 access 可用
- refresh 重放：已轮换的旧 refresh 再使用 → 401
- 改密 → 旧 access 立即失效（sv 不匹配）
- 管理员禁用账号 → 该用户 token 立即失效
- logout → refresh session 吊销 + cookie 清除
- 无 cookie 调 refresh → 401
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import AuthSession, User
from app.db.session import async_session_factory


async def _reg(client: AsyncClient, username: str, password: str = "pass123") -> dict:
    """注册并返回 JSON（同时种下 refresh cookie 到 client.cookies）。"""
    r = await client.post("/api/auth/register", json={
        "username": username, "password": password, "nickname": "测试"
    })
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client: AsyncClient, username: str, password: str = "pass123") -> dict:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


class TestLoginSession:
    async def test_login_sets_refresh_cookie(self, client):
        data = await _login(client, "admin", "123456")
        assert data["access_token"]
        assert "refresh_token" in client.cookies, "登录应种 refresh HttpOnly cookie"
        r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
        assert r.status_code == 200, r.text
        assert r.json()["username"] == "admin"

    async def test_access_token_is_short_lived(self, client):
        # 通过解码 token 验证 exp 不超 15 分钟
        import jwt

        from app.core.config import settings

        data = await _login(client, "admin", "123456")
        payload = jwt.decode(data["access_token"], settings.jwt_secret, algorithms=["HS256"])
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        remain = payload["exp"] - now.timestamp()
        assert 0 < remain <= 15 * 60 + 5, f"access 应在 15 分钟内过期, 剩余 {remain}s"

    async def test_access_token_has_session_version(self, client):
        import jwt

        from app.core.config import settings

        data = await _login(client, "admin", "123456")
        payload = jwt.decode(data["access_token"], settings.jwt_secret, algorithms=["HS256"])
        assert "sv" in payload, "access token 应携带 session_version(sv)"


class TestRefreshRotation:
    async def test_refresh_rotates_and_returns_new_access(self, client):
        await _login(client, "admin", "123456")
        old_cookie = client.cookies.get("refresh_token")
        assert old_cookie

        r = await client.post("/api/auth/refresh")
        assert r.status_code == 200, r.text
        new_cookie = client.cookies.get("refresh_token")
        assert new_cookie and new_cookie != old_cookie, "refresh 应轮换为新的随机串"

        # 新 access 可用
        r2 = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
        assert r2.status_code == 200, r2.text

        # 旧 refresh 已被吊销：手动恢复旧 cookie 再 refresh → 401
        client.cookies.set("refresh_token", old_cookie)
        r3 = await client.post("/api/auth/refresh")
        assert r3.status_code == 401, "已轮换的旧 refresh 重放应被拒绝"
        client.cookies.set("refresh_token", new_cookie)  # 恢复

    async def test_refresh_without_cookie_401(self, client):
        # 清掉 cookie 再 refresh
        client.cookies.clear()
        r = await client.post("/api/auth/refresh")
        assert r.status_code == 401, r.text


class TestChangePasswordInvalidatesTokens:
    async def test_change_password_revokes_old_access(self, client):
        # 管理员登录
        await _login(client, "admin", "123456")

        # 注册普通用户并登录
        await _reg(client, "sv_user1")
        data = await _login(client, "sv_user1")
        old_access = data["access_token"]

        # 改密
        r = await client.put(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {old_access}"},
            json={"old_password": "pass123", "new_password": "newpass456"},
        )
        assert r.status_code == 200, r.text

        # 旧 access 立即失效（sv 已 +1）
        r2 = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_access}"})
        assert r2.status_code == 401, "改密后旧 access 应失效"

        # 旧 refresh 也失效（session 已全部吊销）
        r3 = await client.post("/api/auth/refresh")
        assert r3.status_code == 401, "改密后旧 refresh 应失效"

    async def test_password_change_check_db_session_revoked(self, client):
        await _reg(client, "sv_user2")
        await _login(client, "sv_user2")
        async with async_session_factory() as db:
            sessions = (await db.execute(select(AuthSession))).scalars().all()
            assert len(sessions) >= 1, "登录后应有 session 记录"
            user = await db.scalar(select(User).where(User.username == "sv_user2"))
            sv_before = user.session_version
            sid = sessions[-1].id

        # 改密
        data = await _login(client, "sv_user2")
        r = await client.put(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {data['access_token']}"},
            json={"old_password": "pass123", "new_password": "newpass456"},
        )
        assert r.status_code == 200

        async with async_session_factory() as db:
            user = await db.scalar(select(User).where(User.username == "sv_user2"))
            assert user.session_version == sv_before + 1, "改密后 session_version 应 +1"
            sess = await db.get(AuthSession, sid)
            assert sess.revoked_at is not None, "改密后旧 session 应吊销"


class TestDisableInvalidatesTokens:
    async def test_disable_user_invalidates_issued_token(self, client):
        await _reg(client, "disable_me")
        user_data = await _login(client, "disable_me")
        user_token = user_data["access_token"]

        # 管理员禁用该用户
        admin_data = await _login(client, "admin", "123456")
        admin_headers = {"Authorization": f"Bearer {admin_data['access_token']}"}
        # 用 q 过滤精确命中，避免前 20 个用户分页里没有刚注册的 disable_me
        users = await client.get("/api/admin/users", headers=admin_headers, params={"q": "disable_me"})
        matches = [u for u in users.json()["items"] if u["username"] == "disable_me"]
        assert matches, f"应能找到 disable_me, 实得 {users.json()}"
        uid = matches[0]["id"]
        r = await client.patch(
            f"/api/admin/users/{uid}",
            headers=admin_headers,
            json={"is_active": False},
        )
        assert r.status_code == 200, r.text

        # 被禁用用户的旧 token 立即失效
        r2 = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {user_token}"})
        assert r2.status_code in (401, 403), f"禁用后 token 应失效, 实得 {r2.status_code}"

        # 被禁用用户 login → 403
        r3 = await client.post("/api/auth/login", json={"username": "disable_me", "password": "pass123"})
        assert r3.status_code == 403, r3.text


class TestLogout:
    async def test_logout_revokes_session_and_clears_cookie(self, client, app_ctx):
        # 用独立 client 隔离 cookie jar，避免污染共享 client
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app_ctx), base_url="http://test") as c:
            data = await _reg(c, "logout_me")
            assert data["access_token"]
            cookie_before = c.cookies.get("refresh_token")
            assert cookie_before

            r = await c.post("/api/auth/logout")
            assert r.status_code == 204
            assert c.cookies.get("refresh_token") is None, "logout 应清 cookie"

            # 已吊销的 refresh 再使用 → 401
            c.cookies.set("refresh_token", cookie_before)
            r2 = await c.post("/api/auth/refresh")
            assert r2.status_code == 401, "logout 后旧 refresh 应失效"

    async def test_register_also_sets_cookie(self, client):
        data = await _reg(client, "reg_cookie_user")
        assert data["access_token"]
        assert client.cookies.get("refresh_token"), "注册也应种 refresh cookie"
