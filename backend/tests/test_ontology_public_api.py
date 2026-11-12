"""本体对外接口（/api/v1/ontology/*）：鉴权、契约、事实包。"""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.config import settings

BASE = "/api/v1/ontology"


@pytest.fixture
def external_key(monkeypatch):
    monkeypatch.setattr(settings, "external_api_key", "test-ontology-key")
    return "test-ontology-key"


@pytest_asyncio.fixture
async def sample_param(client, admin_headers, _ensure_test_tables):
    """造一个带证据的参数实例，测完删掉。"""
    payload = {
        "entity_type": "Parameter",
        "name": "t5_sm",
        "kb_id": 0,
        "props": {"display_zh": "自由水蓄水能力", "group": "水源划分", "unit": "mm",
                  "range_low": 5, "range_high": 50, "initial": 10},
        "evidence": [{"type": "手册", "note": "手册参数表", "doc_id": 20, "chunk_id": 28192,
                      "traceable": True}],
    }
    r = await client.post("/api/admin/ontology/instances", headers=admin_headers, json=payload)
    assert r.status_code == 201, r.text
    yield r.json()
    await client.delete(f"/api/admin/ontology/instances/{r.json()['id']}", headers=admin_headers)


class TestAuth:
    async def test_disabled_when_not_configured(self, client, monkeypatch):
        monkeypatch.setattr(settings, "external_api_key", "")
        r = await client.get(f"{BASE}/types")
        assert r.status_code == 503
        assert r.json()["code"] == "EXTERNAL_API_DISABLED"

    async def test_missing_key(self, client, external_key):
        r = await client.get(f"{BASE}/types")
        assert r.status_code == 401

    async def test_wrong_key(self, client, external_key):
        r = await client.get(f"{BASE}/types", headers={"X-API-Key": "bad"})
        assert r.status_code == 401

    async def test_user_jwt_not_accepted(self, client, external_key, user_headers):
        r = await client.get(f"{BASE}/types", headers=user_headers)
        assert r.status_code == 401


class TestReadEndpoints:
    async def test_types(self, client, external_key):
        r = await client.get(f"{BASE}/types", headers={"X-API-Key": external_key})
        assert r.status_code == 200
        data = r.json()
        assert data["profile_id"] == "tuning"
        assert {e["name"] for e in data["entity_types"]} >= {"Parameter", "Basin"}

    async def test_params_returns_card(self, client, external_key, sample_param):
        r = await client.get(
            f"{BASE}/params", headers={"X-API-Key": external_key}, params={"name": "t5_sm"}
        )
        assert r.status_code == 200
        cards = r.json()
        assert len(cards) == 1
        assert cards[0]["range"] == [5.0, 50.0]
        assert cards[0]["evidence_level"] == "traceable"

    async def test_search_links_entity(self, client, external_key, sample_param):
        r = await client.get(
            f"{BASE}/search", headers={"X-API-Key": external_key}, params={"q": "t5_sm 取多少"}
        )
        assert r.status_code == 200
        hits = r.json()
        assert any(h["name"] == "t5_sm" and h["source"] == "instance" for h in hits)

    async def test_entity_detail(self, client, external_key, sample_param):
        r = await client.get(
            f"{BASE}/entities/{sample_param['uid']}", headers={"X-API-Key": external_key}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["instance"]["name"] == "t5_sm"
        assert data["evidence"][0]["note"] == "手册参数表"

    async def test_entity_not_found(self, client, external_key):
        r = await client.get(f"{BASE}/entities/pn:nope", headers={"X-API-Key": external_key})
        assert r.status_code == 404

    async def test_neighbors_503_when_graph_disabled(self, client, external_key):
        """测试环境 GRAPH_ENABLED=false → 503（明确告知未启用，而不是空结果）。"""
        r = await client.get(f"{BASE}/neighbors/pn:x", headers={"X-API-Key": external_key})
        assert r.status_code == 503
        assert r.json()["code"] == "GRAPH_DISABLED"


class TestAsk:
    async def test_ask_returns_fact_pack(self, client, external_key, sample_param):
        r = await client.post(
            f"{BASE}/ask",
            headers={"X-API-Key": external_key},
            json={"question": "t5_sm 这个参数怎么取", "min_facts": 1},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["question"]
        assert "facts" in data and "fallback_used" in data
        assert any(f["kind"] == "parameter_prior" for f in data["facts"])
        assert "ontology" in data["sources"]

    async def test_ask_validates_question(self, client, external_key):
        r = await client.post(
            f"{BASE}/ask", headers={"X-API-Key": external_key}, json={"question": ""}
        )
        assert r.status_code == 422
