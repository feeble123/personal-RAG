"""单元 1：对外检索 API —— 鉴权 / 契约 / 参数校验。

对外 API 用 X-API-Key（服务间调用），不复用用户 JWT。
契约沿用 retriever.py 冻结字段，测试用 monkeypatch 打桩检索，避免依赖真实入库。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import retriever


@pytest.fixture
def external_key(monkeypatch):
    """启用外部 API 并返回测试 key。"""
    monkeypatch.setattr(settings, "external_api_key", "test-external-key-123")
    return "test-external-key-123"


class TestExternalAuth:
    async def test_disabled_when_not_configured(self, client, monkeypatch):
        """未配置 EXTERNAL_API_KEY → 503（功能未启用，不允许空 key 放行）。"""
        monkeypatch.setattr(settings, "external_api_key", "")
        r = await client.post("/api/v1/search", json={"query": "明渠均匀流"})
        assert r.status_code == 503
        assert r.json()["code"] == "EXTERNAL_API_DISABLED"

    async def test_missing_key(self, client, external_key):
        r = await client.post("/api/v1/search", json={"query": "明渠均匀流"})
        assert r.status_code == 401

    async def test_wrong_key(self, client, external_key):
        r = await client.post(
            "/api/v1/search",
            json={"query": "明渠均匀流"},
            headers={"X-API-Key": "wrong-key"},
        )
        assert r.status_code == 401

    async def test_user_jwt_not_accepted(self, client, external_key, admin_headers):
        """外部 API 不接受用户 JWT（必须用 X-API-Key）。"""
        r = await client.post(
            "/api/v1/search",
            json={"query": "明渠均匀流"},
            headers=admin_headers,
        )
        assert r.status_code == 401


class TestExternalSearch:
    async def test_validation(self, client, external_key):
        h = {"X-API-Key": external_key}
        r = await client.post("/api/v1/search", json={"query": ""}, headers=h)
        assert r.status_code == 422
        r = await client.post("/api/v1/search", json={"query": "x", "top_k": 99}, headers=h)
        assert r.status_code == 422
        r = await client.post("/api/v1/search", json={"query": "x", "top_k": 0}, headers=h)
        assert r.status_code == 422

    async def test_contract(self, client, external_key, monkeypatch):
        """返回结构必须与冻结契约完全一致。"""
        async def _fake_retrieve(query, top_k=5, kb_id=None, doc_ids=None):
            return [
                retriever.RetrievalResult(
                    text="明渠均匀流的形成条件包括：……",
                    score=0.93,
                    source=retriever.RetrievalSource(
                        document_name="水力学.pdf",
                        document_type="textbook",
                        section="7.4 明渠均匀流",
                        page=215,
                        clause_no=None,
                        formula_no=None,
                        block_type="text",
                        doc_id=5,
                        chunk_id=1882,
                    ),
                )
            ]

        monkeypatch.setattr(retriever, "retrieve", _fake_retrieve)
        r = await client.post(
            "/api/v1/search",
            json={"query": "明渠均匀流形成条件", "top_k": 1, "kb_id": 3},
            headers={"X-API-Key": external_key},
        )
        assert r.status_code == 200
        data = r.json()
        assert list(data.keys()) == ["results"]
        item = data["results"][0]
        assert set(item.keys()) == {"text", "score", "source"}
        assert item["text"].startswith("明渠均匀流")
        assert item["score"] == 0.93
        assert item["source"] == {
            "document_name": "水力学.pdf",
            "document_type": "textbook",
            "section": "7.4 明渠均匀流",
            "page": 215,
            "clause_no": None,
            "formula_no": None,
            "block_type": "text",
            "doc_id": 5,
            "chunk_id": 1882,
        }

    async def test_empty_results(self, client, external_key, monkeypatch):
        async def _empty(query, top_k=5, kb_id=None, doc_ids=None):
            return []

        monkeypatch.setattr(retriever, "retrieve", _empty)
        r = await client.post(
            "/api/v1/search",
            json={"query": "完全无关的问题"},
            headers={"X-API-Key": external_key},
        )
        assert r.status_code == 200
        assert r.json() == {"results": []}


class TestExternalKnowledgeBases:
    async def test_requires_key(self, client, external_key):
        r = await client.get("/api/v1/knowledge-bases")
        assert r.status_code == 401

    async def test_list(self, client, external_key):
        r = await client.get(
            "/api/v1/knowledge-bases", headers={"X-API-Key": external_key}
        )
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert isinstance(data["items"], list)
