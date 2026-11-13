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
        # 预演要报「会新增 1」——只报 0 的话用户以为导入没生效（用户实测反馈）
        assert data["created"] == 1
        assert data["preview"][0]["name"] == "t4_ke"

        # 但预演必须**一个字都不落库**
        r = await client.get(
            f"{BASE}/instances", headers=admin_headers, params={"entity_type": "Parameter"}
        )
        assert not any(item["name"] == "t4_ke" for item in r.json()), "预演不该写库"

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


class TestGraphOverview:
    """前端画图用的全览接口。"""

    async def test_requires_admin(self, client, user_headers):
        r = await client.get(f"{BASE}/graph", headers=user_headers)
        assert r.status_code == 403

    async def test_returns_instances_as_nodes(self, client, admin_headers, cleanup_instances):
        created = await client.post(
            f"{BASE}/instances", headers=admin_headers, json=_instance_payload("t4_graph_sm")
        )
        assert created.status_code == 201, created.text
        uid = created.json()["uid"]

        r = await client.get(f"{BASE}/graph", headers=admin_headers, params={"node_limit": 500})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["profile_id"] == "tuning"
        node = next(n for n in data["nodes"] if n["uid"] == uid)
        assert node["name"] == "t4_graph_sm"
        assert node["display"] == "测试参数"  # 中文名优先展示
        assert node["evidence_count"] == 1
        assert isinstance(data["edges"], list)

    async def test_graph_disabled_reports_honestly(self, client, admin_headers):
        """测试环境图库关闭：节点照常给（来自 PG），但要明确说边不可用。"""
        r = await client.get(f"{BASE}/graph", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["graph_available"] is False
        assert "note" in data and "图库未启用" in data["note"]


class TestRelations:
    """关系的真相源在 PG：建/改/删都要能回溯，且改错了能修。"""

    async def _two_params(self, client, admin_headers):
        a = await client.post(
            f"{BASE}/instances", headers=admin_headers, json=_instance_payload("t4_rel_a")
        )
        b = await client.post(
            f"{BASE}/instances", headers=admin_headers, json=_instance_payload("t4_rel_b")
        )
        assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
        return a.json(), b.json()

    async def test_create_and_list(self, client, admin_headers, cleanup_instances):
        a, b = await self._two_params(client, admin_headers)
        payload = {
            "subject_uid": a["uid"],
            "predicate": "PART_OF",
            "object_uid": b["uid"],
            "evidence": [{"type": "教材", "note": "表5.2"}],
        }
        r = await client.post(f"{BASE}/relations", headers=admin_headers, json=payload)
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["predicate"] == "PART_OF"

        listed = await client.get(
            f"{BASE}/relations", headers=admin_headers, params={"touch_uid": a["uid"]}
        )
        assert listed.status_code == 200
        assert any(x["id"] == created["id"] for x in listed.json())

        await client.delete(f"{BASE}/relations/{created['id']}", headers=admin_headers)

    async def test_unknown_predicate_rejected(self, client, admin_headers, cleanup_instances):
        a, b = await self._two_params(client, admin_headers)
        r = await client.post(
            f"{BASE}/relations",
            headers=admin_headers,
            json={"subject_uid": a["uid"], "predicate": "NOT_A_RELATION", "object_uid": b["uid"]},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "ONTOLOGY_INVALID_RELATION"

    async def test_endpoint_missing(self, client, admin_headers, cleanup_instances):
        a, _ = await self._two_params(client, admin_headers)
        r = await client.post(
            f"{BASE}/relations",
            headers=admin_headers,
            json={"subject_uid": a["uid"], "predicate": "PART_OF", "object_uid": "pn:nope"},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "ONTOLOGY_RELATION_ENDPOINT_MISSING"

    async def test_duplicate_rejected(self, client, admin_headers, cleanup_instances):
        a, b = await self._two_params(client, admin_headers)
        payload = {"subject_uid": a["uid"], "predicate": "PART_OF", "object_uid": b["uid"]}
        first = await client.post(f"{BASE}/relations", headers=admin_headers, json=payload)
        assert first.status_code == 201
        second = await client.post(f"{BASE}/relations", headers=admin_headers, json=payload)
        assert second.status_code == 409
        await client.delete(f"{BASE}/relations/{first.json()['id']}", headers=admin_headers)

    async def test_patch_and_delete(self, client, admin_headers, cleanup_instances):
        a, b = await self._two_params(client, admin_headers)
        created = await client.post(
            f"{BASE}/relations",
            headers=admin_headers,
            json={"subject_uid": a["uid"], "predicate": "PART_OF", "object_uid": b["uid"]},
        )
        rid = created.json()["id"]

        patched = await client.patch(
            f"{BASE}/relations/{rid}",
            headers=admin_headers,
            json={"evidence": [{"type": "手册", "note": "新增依据"}], "status": "archived"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "archived"
        assert patched.json()["evidence"][0]["note"] == "新增依据"

        deleted = await client.delete(f"{BASE}/relations/{rid}", headers=admin_headers)
        assert deleted.status_code == 204
        gone = await client.patch(
            f"{BASE}/relations/{rid}", headers=admin_headers, json={"status": "active"}
        )
        assert gone.status_code == 404

    async def test_delete_instance_cascades_relations(self, client, admin_headers, cleanup_instances):
        a, b = await self._two_params(client, admin_headers)
        created = await client.post(
            f"{BASE}/relations",
            headers=admin_headers,
            json={"subject_uid": a["uid"], "predicate": "PART_OF", "object_uid": b["uid"]},
        )
        assert created.status_code == 201

        await client.delete(f"{BASE}/instances/{a['id']}", headers=admin_headers)
        left = await client.get(
            f"{BASE}/relations", headers=admin_headers, params={"touch_uid": a["uid"]}
        )
        assert left.json() == []
