"""本体管理接口：鉴权、实例 CRUD、推荐卡、参数表导入。"""
from __future__ import annotations

import io

import pytest
import pytest_asyncio
from openpyxl import Workbook

from app.services.graph.ontology.params import HEADERS

BASE = "/api/admin/ontology"


@pytest_asyncio.fixture
async def cleanup_instances(_ensure_test_tables):
    """清掉本文件造的测试实例（按名称前缀识别）。"""
    yield
    from sqlalchemy import delete

    from app.db.models import OntologyInstance
    from app.db.session import async_session_factory

    async with async_session_factory() as s:
        await s.execute(delete(OntologyInstance).where(OntologyInstance.name.like("t4%")))
        await s.commit()


def _instance_payload(name: str = "t4_sm", **kw) -> dict:
    payload = {
        "entity_type": "Parameter",
        "name": name,
        "kb_id": 0,
        "props": {"display_zh": "测试参数", "group": "水源划分", "unit": "mm"},
        "evidence": [{"type": "教材/手册", "note": "测试"}],
    }
    payload.update(kw)
    return payload


class TestAuth:
    async def test_requires_login(self, client):
        r = await client.get(f"{BASE}/instances")
        assert r.status_code == 401

    async def test_requires_admin(self, client, user_headers):
        r = await client.get(f"{BASE}/instances", headers=user_headers)
        assert r.status_code == 403


class TestTypes:
    async def test_types_snapshot(self, client, admin_headers):
        r = await client.get(f"{BASE}/types", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["profile_id"] == "tuning"
        names = {e["name"] for e in data["entity_types"]}
        assert {"Parameter", "Basin", "Metric"} <= names


class TestInstanceCrud:
    async def test_create_reject_invalid(self, client, admin_headers, cleanup_instances):
        r = await client.post(
            f"{BASE}/instances",
            headers=admin_headers,
            json=_instance_payload("t4_bad", props={"unit": "mm"}),
        )
        assert r.status_code == 400
        body = r.json()
        assert body["code"] == "ONTOLOGY_INVALID_INSTANCE"
        assert "group" in body["message"]

    async def test_full_lifecycle(self, client, admin_headers, cleanup_instances):
        r = await client.post(f"{BASE}/instances", headers=admin_headers, json=_instance_payload())
        assert r.status_code == 201, r.text
        row = r.json()
        assert row["uid"].startswith("pn:")
        assert row["profile_id"] == "tuning"

        # 重复创建 → 409
        r = await client.post(f"{BASE}/instances", headers=admin_headers, json=_instance_payload())
        assert r.status_code == 409

        # 列表能查到
        r = await client.get(f"{BASE}/instances", headers=admin_headers, params={"entity_type": "Parameter"})
        assert any(item["id"] == row["id"] for item in r.json())

        # 修改
        r = await client.patch(
            f"{BASE}/instances/{row['id']}",
            headers=admin_headers,
            json={"props": {**row["props"], "initial": 18.5}},
        )
        assert r.status_code == 200
        assert r.json()["props"]["initial"] == 18.5

        # 删除
        r = await client.delete(f"{BASE}/instances/{row['id']}", headers=admin_headers)
        assert r.status_code == 204
        r = await client.get(f"{BASE}/instances", headers=admin_headers, params={"entity_type": "Parameter"})
        assert not any(item["id"] == row["id"] for item in r.json())

    async def test_patch_missing_returns_404(self, client, admin_headers):
        r = await client.patch(f"{BASE}/instances/999999", headers=admin_headers, json={"status": "archived"})
        assert r.status_code == 404


class TestParamCards:
    async def test_card_narrows_by_condition(self, client, admin_headers, cleanup_instances):
        payload = _instance_payload(
            "t4_sm2",
            props={
                "display_zh": "自由水蓄水能力",
                "group": "水源划分",
                "unit": "mm",
                "range_low": 15,
                "range_high": 30,
            },
            ranges=[{"low": 15, "high": 25, "when": {"climate": "湿润"}}],
        )
        r = await client.post(f"{BASE}/instances", headers=admin_headers, json=payload)
        assert r.status_code == 201, r.text

        r = await client.get(
            f"{BASE}/params", headers=admin_headers, params={"name": "t4_sm2", "climate": "湿润"}
        )
        assert r.status_code == 200
        cards = r.json()
        assert len(cards) == 1
        assert cards[0]["range_level"] == "conditional"
        assert cards[0]["range"] == [15, 25]

        r = await client.get(f"{BASE}/params", headers=admin_headers, params={"name": "t4_sm2", "climate": "干旱"})
        assert r.json()[0]["range_level"] == "general"

    async def test_no_evidence_no_numbers(self, client, admin_headers, cleanup_instances):
        payload = _instance_payload(
            "t4_noev",
            props={"group": "产流", "range_low": 1, "range_high": 2},
            evidence=[],
        )
        r = await client.post(f"{BASE}/instances", headers=admin_headers, json=payload)
        assert r.status_code == 201

        r = await client.get(f"{BASE}/params", headers=admin_headers, params={"name": "t4_noev"})
        card = r.json()[0]
        assert card["range"] is None and card["initial"] is None
        assert card["confidence"] == 0.0


class TestImport:
    def _xlsx(self) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.append(list(HEADERS))
        ws.append([
            "t4_ke", "测试蒸散发系数", "蒸散发", "-",
            0.5, 1.5, 0.8, 1.1, "", 1.0, "高", "教材/手册", "测试来源", "接口测试",
        ])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    async def test_dry_run_then_apply(self, client, admin_headers, cleanup_instances):
        content = self._xlsx()
        files = {
            "file": (
                "p.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        }
        r = await client.post(f"{BASE}/params/import", headers=admin_headers, files=files)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["dry_run"] is True
        assert data["rows"] == 1 and data["drafts"] == 1
        assert data["created"] == 0
        assert data["preview"][0]["name"] == "t4_ke"

        files = {
            "file": (
                "p.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        }
        r = await client.post(
            f"{BASE}/params/import", headers=admin_headers, files=files, params={"dry_run": "false"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 1

        r = await client.get(f"{BASE}/instances", headers=admin_headers, params={"entity_type": "Parameter"})
        assert any(item["name"] == "t4_ke" for item in r.json())

    async def test_bad_file_type(self, client, admin_headers):
        files = {"file": ("p.txt", b"hello", "text/plain")}
        r = await client.post(f"{BASE}/params/import", headers=admin_headers, files=files)
        assert r.status_code == 400
