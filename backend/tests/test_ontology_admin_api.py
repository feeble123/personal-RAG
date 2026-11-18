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
    from sqlalchemy import delete, or_, select

    from app.db.models import OntologyInstance, OntologyProposal, OntologyRelation
    from app.db.session import async_session_factory

    async with async_session_factory() as s:
        # uid 是 `pm:<sha1>`，按名字 like 匹配不到——先取 uid 再删提案和边
        uids = [
            r[0]
            for r in (
                await s.execute(
                    select(OntologyInstance.uid).where(OntologyInstance.name.like("t4%"))
                )
            ).all()
        ]
        if uids:
            # 待审区也要清：锚定用例会写提案，留着会污染别的用例对「待审 N 条」的断言
            await s.execute(
                delete(OntologyProposal).where(OntologyProposal.instance_uid.in_(uids))
            )
            # 运行记录会造 USED/ACHIEVED 边，不清会留下悬空边
            await s.execute(
                delete(OntologyRelation).where(
                    or_(
                        OntologyRelation.subject_uid.in_(uids),
                        OntologyRelation.object_uid.in_(uids),
                    )
                )
            )
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


class TestLibraries:
    """库选择器上的「N 节点」：必须等于切进去能看见几个，不能凭图库统计一头算。

    用户实测的 bug：四个库全显示「0 节点」，可调参本体里明明有 15 个参数。
    原因是 Neo4j 没启动时 `store.stats()` 降级返回空，按类型求和恒为 0。
    """

    async def test_node_count_comes_from_instances_when_graph_is_down(
        self, client, admin_headers, cleanup_instances
    ):
        for name in ("t4_lib_a", "t4_lib_b"):
            r = await client.post(
                f"{BASE}/instances", headers=admin_headers, json=_instance_payload(name)
            )
            assert r.status_code == 201, r.text

        r = await client.get(f"{BASE}/libraries", headers=admin_headers)
        assert r.status_code == 200, r.text
        libs = {lib["profile_id"]: lib for lib in r.json()}
        tuning = libs["tuning"]
        assert tuning["instance_count"] >= 2
        # 图库关着 / 没同步 → 节点数就该等于实例数，而不是 0
        assert tuning["node_count"] == tuning["instance_count"]

    async def test_node_count_keeps_graph_only_nodes(
        self, client, admin_headers, monkeypatch
    ):
        """防洪链那批节点直接导进了图、没有实例行：只数 PG 会把它们漏成 0。"""

        class _Store:
            async def stats(self, kb_id=None):
                return {
                    "nodes": {"total": 25, "by_type": {"engineering_object": 25}},
                    "edges": {"total": 0, "by_type": {}},
                }

        monkeypatch.setattr(
            "app.services.graph.store.get_graph_store", lambda: _Store()
        )
        r = await client.get(f"{BASE}/libraries", headers=admin_headers)
        assert r.status_code == 200, r.text
        libs = {lib["profile_id"]: lib for lib in r.json()}
        assert libs["flood_chain"]["node_count"] == 25
        # 没声明这个类型的库不能被别人的节点算进来
        assert libs["process_skeleton"]["node_count"] == 0


class TestEvidenceTraceability:
    """P1：手填的「文档名 + 摘录」也要能锚定到具体切片，界面才点得回原文。

    以前只有参数表导入会做这个解析，实例表单和实例 Excel 导入都只是存一段文本，
    用户看到的「doc 20 · 第5页」既点不开也不保证真有这段。
    """

    async def _create(self, client, admin_headers, kb_id, evidence, name="t4_ev"):
        payload = _instance_payload(name, kb_id=kb_id)
        payload["evidence"] = evidence
        r = await client.post(f"{BASE}/instances", headers=admin_headers, json=payload)
        assert r.status_code == 201, r.text
        return r.json()

    async def test_manual_evidence_resolves_to_chunk(
        self, client, admin_headers, sample_kb, cleanup_instances
    ):
        kb_id, _doc_id = sample_kb
        data = await self._create(
            client,
            admin_headers,
            kb_id,
            [
                {
                    "type": "手册",
                    "note": "明渠均匀流的形成条件",
                    "doc_name": "demo.md",
                    # 摘录要 ≥6 字才用于定位（更短匹配噪音太大，宁可不定位）
                    "snippet": "明渠均匀流的形成条件",
                    "attributes": ["unit"],
                }
            ],
        )
        ev = data["evidence"][0]
        assert ev["doc_id"] and ev["chunk_id"]
        assert ev["traceable"] is True
        assert ev["resolved_by"] == "chunk"
        assert ev["attributes"] == ["unit"]  # 属性级证据

        # 点开依据能看到原文
        r = await client.get(f"{BASE}/evidence/{ev['chunk_id']}", headers=admin_headers)
        assert r.status_code == 200, r.text
        chunk = r.json()
        assert "明渠均匀流" in chunk["content"]
        assert chunk["doc_name"] == "demo.md"

    async def test_unknown_document_marked_untraceable(
        self, client, admin_headers, sample_kb, cleanup_instances
    ):
        """找不到就如实标未溯源，不硬指一个切片，也不丢用户写的说明。"""
        kb_id, _doc_id = sample_kb
        data = await self._create(
            client,
            admin_headers,
            kb_id,
            [{"type": "手册", "note": "现场台账", "doc_name": "根本没有这本册子.pdf"}],
            name="t4_ev_missing",
        )
        ev = data["evidence"][0]
        assert ev["traceable"] is False
        assert ev["resolved_by"] == "none"
        assert ev["note"] == "现场台账"  # 原文不丢
        assert "chunk_id" not in ev or ev["chunk_id"] is None

    async def test_missing_chunk_returns_404(self, client, admin_headers):
        r = await client.get(f"{BASE}/evidence/999999", headers=admin_headers)
        assert r.status_code == 404


CHUNK_TEXT = "4.模型参数\n测试参数 自由水蓄水容量 5～50 默认 10\n"


class _FakeLLM:
    """假模型：直接回一段固定 JSON（锚定用例只验证程序侧的闸门）。"""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def ainvoke(self, _messages):
        from types import SimpleNamespace

        return SimpleNamespace(content=self.payload)


class TestEvidenceAnchoring:
    """P2：给已有实例找原文依据。**不改值**，东西全进待审区。

    用户的口径：实例是人按业务链条梳理的，程序只许指出"哪段原文支持它"，
    值要不要采用由人另点「采用原文值」。
    """

    async def _instance(self, client, admin_headers, name="t4_anchor"):
        r = await client.post(f"{BASE}/instances", headers=admin_headers, json=_instance_payload(name))
        assert r.status_code == 201, r.text
        return r.json()

    def _stub(self, monkeypatch, items: list[dict]):
        import json
        from types import SimpleNamespace

        from app.services.graph.ontology import anchoring

        cand = anchoring.Candidate(
            chunk_id=901, doc_id=1, doc_name="新安江模型手册.pdf", page=4,
            section="4.模型参数", content=CHUNK_TEXT,
        )

        async def fake_candidates(db, query, *, kb_id, top_k=6, doc_ids=None):
            return [cand]

        monkeypatch.setattr(anchoring, "candidate_chunks", fake_candidates)
        payload = json.dumps({"items": items}, ensure_ascii=False)
        monkeypatch.setattr(
            "app.services.chat.build_chat_model", lambda *a, **kw: _FakeLLM(payload)
        )

    async def _props(self, client, admin_headers, uid: str) -> dict:
        r = await client.get(f"{BASE}/instances", headers=admin_headers)
        row = next(i for i in r.json() if i["uid"] == uid)
        return row

    async def test_anchor_only_attaches_evidence_unless_adopted(
        self, client, admin_headers, cleanup_instances, monkeypatch
    ):
        inst = await self._instance(client, admin_headers)
        self._stub(
            monkeypatch,
            [
                # 只是给出处，不改中文名
                {"chunk": 1, "attribute": "display_zh", "quote": "测试参数 自由水蓄水容量", "value": "自由水蓄水容量"},
                # 原文里确实写了 5，但实例本来没这个字段
                {"chunk": 1, "attribute": "range_low", "quote": "测试参数 自由水蓄水容量 5～50 默认 10", "value": 5},
            ],
        )

        r = await client.post(
            f"{BASE}/anchor/run",
            headers=admin_headers,
            json={"profile_id": "tuning", "instance_uids": [inst["uid"]]},
        )
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["proposed"] == 2 and run["saved"] == 2 and run["rejected"] == 0

        r = await client.get(
            f"{BASE}/proposals", headers=admin_headers,
            params={"profile": "tuning", "method": "anchor"},
        )
        proposals = {p["attribute"]: p for p in r.json() if p["instance_uid"] == inst["uid"]}
        assert proposals["range_low"]["value"] == 5
        assert proposals["range_low"]["current_value"] is None  # 实例本来没这个字段
        assert proposals["display_zh"]["current_value"] == "测试参数"

        # 接受第 1 条：只挂证据，值不动
        r = await client.post(
            f"{BASE}/proposals/{proposals['display_zh']['id']}/accept", headers=admin_headers
        )
        assert r.status_code == 200
        row = await self._props(client, admin_headers, inst["uid"])
        assert row["props"]["display_zh"] == "测试参数"  # 一个字都没改
        ev = row["evidence"][-1]
        assert ev["traceable"] is True
        assert ev["attributes"] == ["display_zh"]
        assert ev["chunk_id"] == 901
        assert ev["quote"].startswith("测试参数")

        # 接受第 2 条并选择「采用原文值」：这次才写值
        r = await client.post(
            f"{BASE}/proposals/{proposals['range_low']['id']}/accept",
            headers=admin_headers,
            params={"adopt_value": "true"},
        )
        assert r.status_code == 200
        row = await self._props(client, admin_headers, inst["uid"])
        assert row["props"]["range_low"] == 5

    async def test_fabricated_quote_is_dropped(
        self, client, admin_headers, cleanup_instances, monkeypatch
    ):
        """模型编一句原文里没有的话 → 整条丢弃，不进待审区。"""
        inst = await self._instance(client, admin_headers, name="t4_anchor_fake")
        self._stub(
            monkeypatch,
            [{"chunk": 1, "attribute": "unit", "quote": "本参数经验值一般取 0.9", "value": 0.9}],
        )
        r = await client.post(
            f"{BASE}/anchor/run",
            headers=admin_headers,
            json={"profile_id": "tuning", "instance_uids": [inst["uid"]]},
        )
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["saved"] == 0
        assert run["rejected"] == 1
        assert any("原文里找不到" in k for k in run["reject_reasons"])


class TestRunRecords:
    """O8-2：一次率定 → Run 节点 + 用到的参数 + 达到的指标。"""

    async def test_record_and_list(self, client, admin_headers, cleanup_instances):
        created = await client.post(
            f"{BASE}/instances", headers=admin_headers, json=_instance_payload("t4_run_sm")
        )
        assert created.status_code == 201, created.text

        payload = {
            "profile_id": "tuning",
            "run_id": "t4-run-1",
            "started_at": "2026-11-18T10:00:00",
            "strategy": "SCE-UA",
            "objective": "NSE 最大化",
            "parameters": {"t4_run_sm": 22, "t4_missing": 1},
            "metrics": {"t4_NSE": 0.82},
        }
        r = await client.post(f"{BASE}/runs", headers=admin_headers, json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["created"] is True
        assert [u["parameter"] for u in data["used"]] == ["t4_run_sm"]
        assert data["missing_parameters"] == ["t4_missing"]
        assert data["achieved"] == [{"metric": "t4_NSE", "value": 0.82}]
        assert data["created_metrics"] == ["t4_NSE"]

        r = await client.get(f"{BASE}/runs", headers=admin_headers, params={"profile": "tuning"})
        assert r.status_code == 200, r.text
        row = next(x for x in r.json() if x["name"] == "t4-run-1")
        assert row["strategy"] == "SCE-UA"
        assert row["parameters"][0]["value"] == 22
        assert row["metrics"][0]["value"] == 0.82

    async def test_library_without_run_type_is_rejected(self, client, admin_headers):
        r = await client.post(
            f"{BASE}/runs",
            headers=admin_headers,
            json={"profile_id": "process_skeleton", "run_id": "t4-run-x", "parameters": {}},
        )
        assert r.status_code == 400
        assert "缺少类型" in r.json()["message"]


class TestFlowImport:
    """O8-1：一张表把流程链条建好（阶段 → 动作 → 工具 / 数据）。"""

    @pytest_asyncio.fixture(autouse=True)
    async def _cleanup(self, _ensure_test_tables):
        yield
        from sqlalchemy import delete

        from app.db.models import OntologyInstance, OntologyRelation
        from app.db.session import async_session_factory

        async with async_session_factory() as s:
            await s.execute(
                delete(OntologyRelation).where(
                    OntologyRelation.profile_id == "process_skeleton"
                )
            )
            await s.execute(
                delete(OntologyInstance).where(
                    OntologyInstance.profile_id == "process_skeleton"
                )
            )
            await s.commit()

    def _file(self):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["阶段", "阶段序号", "动作", "工具", "工具来源", "需要的数据", "数据格式"])
        ws.append(["数据准备", 1, "下载边界", "站网API", "外部", "边界矢量", "shp"])
        ws.append([None, None, "整理降雨", "整理脚本", "内部", "逐时降雨", "csv"])
        ws.append(["建模", 2, "装配模型", "装配器", "内部", "参数表", "xlsx"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return ("flow.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    async def test_dry_run_then_apply(self, client, admin_headers):
        r = await client.post(
            f"{BASE}/flow/import",
            headers=admin_headers,
            files={"file": self._file()},
            params={"profile": "process_skeleton", "dry_run": "true"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["stages"] == 2 and data["actions"] == 3
        assert data["relations"] == 1 + 3 + 3 + 3  # 阶段链 + 三类悬挂边
        assert data["dry_run"] is True

        r = await client.post(
            f"{BASE}/flow/import",
            headers=admin_headers,
            files={"file": self._file()},
            params={"profile": "process_skeleton", "dry_run": "false"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["created"] >= 8

        r = await client.get(
            f"{BASE}/instances", headers=admin_headers, params={"profile": "process_skeleton"}
        )
        rows = r.json()
        types = {i["entity_type"] for i in rows}
        assert {"Stage", "Action", "Tool", "DataAsset"} <= types
        orders = {
            i["name"]: i["props"].get("order") for i in rows if i["entity_type"] == "Stage"
        }
        assert orders == {"数据准备": 1, "建模": 2}

        r = await client.get(
            f"{BASE}/relations", headers=admin_headers, params={"profile": "process_skeleton"}
        )
        predicates = {x["predicate"] for x in r.json()}
        assert {"NEXT_STAGE", "PERFORMED_BY", "USES_TOOL", "REQUIRES_DATA"} <= predicates

    async def test_rejects_library_without_flow_types(self, client, admin_headers):
        r = await client.post(
            f"{BASE}/flow/import",
            headers=admin_headers,
            files={"file": self._file()},
            params={"profile": "tuning", "dry_run": "true"},
        )
        assert r.status_code == 400
        assert "缺少流程类型" in r.json()["message"]


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
