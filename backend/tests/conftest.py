"""pytest 配置：使用 FAKE embedding/LLM 离线跑通全流程，隔离数据目录。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

# 必须先于 app 导入设置环境（settings 在导入时加载）
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["RERANK_ENABLED"] = "false"  # 集成测试离线，不走 rerank API
os.environ["DEBUG"] = "false"
# P0-1：测试用固定密钥/密码（而非随机 fallback），保证 token/登录测试稳定可复现
os.environ["JWT_SECRET"] = "test-secret-for-unit-tests-not-production"
os.environ["ADMIN_PASSWORD"] = "123456"
# 测试集随功能增长注册用户增多，放开 auth/chat/refresh 限流避免 429 干扰
os.environ["AUTH_RATE_LIMIT"] = "1000/minute"
os.environ["REFRESH_RATE_LIMIT"] = "1000/minute"
os.environ["CHAT_RATE_LIMIT"] = "1000/minute"

# 用临时目录隔离数据，避免污染真实数据
_TMP = tempfile.mkdtemp(prefix="rag_test_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["CHROMA_DIR"] = str(Path(_TMP) / "chroma")
os.environ["UPLOAD_DIR"] = str(Path(_TMP) / "uploads")

from httpx import ASGITransport, AsyncClient  # noqa: E402

import pytest_asyncio

from app.main import app  # noqa: E402

_sample_kb_counter = {"n": 0}
_user_counter = {"n": 0}


@pytest_asyncio.fixture(scope="session")
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest_asyncio.fixture(scope="session")
async def app_ctx():
    """暴露 app 对象，供测试内创建独立 ASGI client（如隔离 cookie jar）。"""
    return app


@pytest_asyncio.fixture
async def admin_headers(client):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def user_headers(client):
    _user_counter["n"] += 1
    r = await client.post(
        "/api/auth/register",
        json={"username": f"tester{_user_counter['n']}", "password": "pass123", "nickname": "测试用户"},
    )
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def sample_kb(client, admin_headers):
    """建库 + 上传示例 md + 等待入库完成。返回 (kb_id, doc_id)。"""
    _sample_kb_counter["n"] += 1
    name = f"水力学示例库{_sample_kb_counter['n']}"
    r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": name})
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]
    md = "# 水利工程基础\n\n## 明渠均匀流\n\n明渠均匀流的形成条件包括：长直棱柱体渠道、正坡、糙率不变、流量恒定。\n\n"
    r = await client.post(
        f"/api/admin/kbs/{kb_id}/documents/upload",
        headers=admin_headers,
        files={"file": ("demo.md", md.encode("utf-8"), "text/markdown")},
    )
    doc_id = r.json()["id"]
    for _ in range(40):
        r = await client.get(f"/api/admin/kbs/{kb_id}/documents", headers=admin_headers)
        status = r.json()["items"][0]["status"]
        if status in ("ready", "failed"):
            break
        await asyncio.sleep(0.2)
    assert status == "ready", f"入库未完成: {status}"
    yield kb_id, doc_id
    # 清理
    await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)
