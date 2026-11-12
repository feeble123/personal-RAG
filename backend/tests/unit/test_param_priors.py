"""参数先验：Excel 解析、条件范围语法、校验、落库（PG）与同步图库。"""
from __future__ import annotations

import pytest
from openpyxl import Workbook

from app.services.graph.ontology import instances as inst_mod
import asyncio

from app.services.graph.ontology.params import (
    HEADERS,
    build_drafts,
    extract_page,
    parse_conditions,
    parse_number,
    read_rows,
    resolve_evidence_with,
)
from app.services.graph.ontology.registry import load_profile, reset_cache
from app.services.graph.store import InMemoryGraphStore

TEST_PROFILE = "test_priors"


@pytest.fixture(scope="module")
def registry():
    reset_cache()
    reg = load_profile("tuning")
    yield reg
    reset_cache()


def _write_excel(path, rows, headers=HEADERS):
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


class TestParsing:
    def test_number_markers(self):
        assert parse_number(None, "x") == (None, None)
        assert parse_number("", "x") == (None, None)
        assert parse_number("待确认", "x") == (None, None)
        assert parse_number(5, "x") == (5.0, None)
        assert parse_number("0.35", "x") == (0.35, None)
        value, warn = parse_number("约0.3", "第2行")
        assert value is None and "不是数字" in warn

    def test_condition_single(self):
        ranges, warns = parse_conditions("climate=湿润 => 0.15~0.30", "第2行")
        assert warns == []
        assert ranges == [{"low": 0.15, "high": 0.30, "when": {"climate": "湿润"}}]

    def test_condition_multiple_lines_and_multi_when(self):
        text = "climate=湿润;flood_type=单峰 => 0.15~0.30\nclimate=半干旱 => 0.2~0.4"
        ranges, warns = parse_conditions(text, "第2行")
        assert warns == []
        assert len(ranges) == 2
        assert ranges[0]["when"] == {"climate": "湿润", "flood_type": "单峰"}

    def test_condition_pipe_separator(self):
        ranges, _ = parse_conditions("climate=湿润 => 1~2 | climate=干旱 => 3~4", "x")
        assert len(ranges) == 2

    def test_condition_missing_arrow(self):
        ranges, warns = parse_conditions("climate=湿润 0.1~0.2", "第3行")
        assert ranges == []
        assert any("缺少 '=>'" in w for w in warns)

    def test_condition_bad_range(self):
        ranges, warns = parse_conditions("climate=湿润 => 上限~下限", "x")
        assert ranges == []
        assert any("里有非数字" in w for w in warns)

    def test_condition_low_greater_than_high(self):
        """下限大于上限是明显的填错，整条丢弃并告警，不进库。"""
        ranges, warns = parse_conditions("climate=湿润 => 0.9~0.1", "x")
        assert ranges == []
        assert any("下限大于上限" in w for w in warns)

    def test_fullwidth_tilde(self):
        ranges, warns = parse_conditions("climate=湿润 => 0.1～0.2", "x")
        assert warns == [] and ranges[0]["high"] == 0.2


class TestEvidenceAndLocator:
    def test_extract_page(self):
        assert extract_page("第53页") == 53
        assert extract_page("P53") == 53
        assert extract_page("2.3 节 第 53 页") == 53
        assert extract_page("附录A") is None
        assert extract_page("") is None

    def test_evidence_carries_doc_fields(self):
        from app.services.graph.ontology.params import PriorRow

        row = PriorRow(
            row_no=2, name="sm", group="水源划分",
            evidence_note="教材推荐", evidence_doc="水力学第5版.pdf",
            evidence_locator="第53页", evidence_snippet="蓄水容量曲线……",
        )
        result = build_drafts([row], _reg(), profile_id=TEST_PROFILE)
        item = result.drafts[0].evidence[0]
        assert item["doc_name"] == "水力学第5版.pdf"
        assert item["page"] == 53
        assert item["snippet"] == "蓄水容量曲线……"
        assert item["resolved_by"] == "none" and item["traceable"] is False

    def test_evidence_without_doc_is_claim(self):
        from app.services.graph.ontology.params import PriorRow

        row = PriorRow(row_no=2, name="sm", group="水源划分", evidence_note="经验值")
        result = build_drafts([row], _reg(), profile_id=TEST_PROFILE)
        assert result.drafts[0].evidence[0].get("doc_name") is None


class _FakeLookup:
    def __init__(self, doc=None, chunk_id=None):
        self._doc = doc
        self._chunk_id = chunk_id

    async def find_document(self, name, kb_id):
        return self._doc

    async def find_chunk(self, doc_id, page, snippet):
        return self._chunk_id


class TestEvidenceResolution:
    def _result(self, **kw):
        from app.services.graph.ontology.params import PriorRow

        row = PriorRow(
            row_no=2, name="sm", group="水源划分", evidence_note="教材",
            evidence_doc=kw.get("doc", "水力学第5版.pdf"),
            evidence_locator=kw.get("locator", "第53页"),
        )
        return build_drafts([row], _reg(), profile_id=TEST_PROFILE)

    def test_resolves_to_chunk(self):
        result = self._result()
        lookup = _FakeLookup(doc={"id": 7, "filename": "水力学第5版.pdf", "kb_id": 1}, chunk_id=999)
        out = asyncio.run(resolve_evidence_with(result, lookup))
        item = out.drafts[0].evidence[0]
        assert item["doc_id"] == 7 and item["chunk_id"] == 999
        assert item["resolved_by"] == "chunk" and item["traceable"] is True

    def test_document_only(self):
        result = self._result()
        lookup = _FakeLookup(doc={"id": 7, "filename": "水力学第5版.pdf", "kb_id": 1}, chunk_id=None)
        out = asyncio.run(resolve_evidence_with(result, lookup))
        item = out.drafts[0].evidence[0]
        assert item["doc_id"] == 7 and "chunk_id" not in item
        assert item["resolved_by"] == "doc" and item["traceable"] is False
        assert any("没定位到具体切片" in w for w in out.warnings)

    def test_document_missing(self):
        result = self._result(doc="不存在的书.pdf")
        out = asyncio.run(resolve_evidence_with(result, _FakeLookup(doc=None)))
        item = out.drafts[0].evidence[0]
        assert item["resolved_by"] == "none" and item["traceable"] is False
        assert any("找不到" in w for w in out.warnings)


def _reg():
    from app.services.graph.ontology.registry import load_profile

    return load_profile("tuning")


class TestReadExcel:
    def test_reads_rows_and_skips_noise(self, tmp_path):
        path = _write_excel(
            tmp_path / "p.xlsx",
            [
                ["sm", "自由水蓄水能力", "水源划分", "mm", "", "", "", "", "", "", "高", "", "", ""],
                ["", "", "", "", "", "", "", "", "", "", "", "", "", ""],
                ["# 这一行是注释", "", "", "", "", "", "", "", "", "", "", "", "", ""],
                ["示例：ke", "示例", "蒸散发", "-", "", "", "", "", "", "", "", "", "", ""],
                ["cg", "地下水消退系数", "汇流", "-", "", "", "", "", "", "", "", "", "", ""],
            ],
        )
        rows = read_rows(path)
        assert [r.name for r in rows] == ["sm", "cg"]
        assert rows[0].row_no == 2  # 表头占第 1 行

    def test_missing_name_header_raises(self, tmp_path):
        path = _write_excel(tmp_path / "bad.xlsx", [["a", "b"]], headers=["中文名", "分组"])
        with pytest.raises(ValueError, match="参数名"):
            read_rows(path)


class TestBuildDrafts:
    def _row(self, **kw):
        from app.services.graph.ontology.params import PriorRow

        base = PriorRow(row_no=2, name="sm", display_zh="自由水蓄水能力", group="水源划分", unit="mm")
        for key, value in kw.items():
            setattr(base, key, value)
        return base

    def test_valid_row(self, registry):
        result = build_drafts([self._row(range_low=15, range_high=30, evidence_note="教材")],
                              registry, profile_id=TEST_PROFILE)
        assert result.errors == []
        draft = result.drafts[0]
        assert draft.props["range_low"] == 15.0
        assert draft.evidence[0]["note"] == "教材"
        assert draft.uid.startswith("pn:")
        assert draft.kb_id == 0

    def test_missing_group_is_error(self, registry):
        result = build_drafts([self._row(group="")], registry, profile_id=TEST_PROFILE)
        assert result.drafts == []
        assert any("group" in e for e in result.errors)

    def test_unknown_group_is_error(self, registry):
        result = build_drafts([self._row(group="不存在的分组")], registry, profile_id=TEST_PROFILE)
        assert result.drafts == []
        assert any("不在枚举" in e for e in result.errors)

    def test_number_without_evidence_warns(self, registry):
        result = build_drafts([self._row(initial=22)], registry, profile_id=TEST_PROFILE)
        assert len(result.drafts) == 1 and result.drafts[0].evidence == []
        assert any("没有「依据」" in w for w in result.warnings)

    def test_bad_number_skipped_but_row_kept(self, registry):
        result = build_drafts([self._row(physical_low="约5")], registry, profile_id=TEST_PROFILE)
        assert len(result.drafts) == 1
        assert "physical_low" not in result.drafts[0].props
        assert any("不是数字" in w for w in result.warnings)

    def test_condition_ranges_kept(self, registry):
        result = build_drafts(
            [self._row(condition_ranges="climate=湿润 => 0.7~0.95")],
            registry,
            profile_id=TEST_PROFILE,
        )
        assert result.drafts[0].ranges == [{"low": 0.7, "high": 0.95, "when": {"climate": "湿润"}}]


class TestPersistence:
    @pytest.fixture
    async def session(self, _ensure_test_tables):
        from app.db.session import async_session_factory

        async with async_session_factory() as s:
            yield s
            from sqlalchemy import delete

            from app.db.models import OntologyInstance

            await s.execute(delete(OntologyInstance).where(OntologyInstance.profile_id == TEST_PROFILE))
            await s.commit()

    def _draft(self, registry, name="sm", **props):
        base = {"display_zh": name, "group": "水源划分", "unit": "mm"}
        base.update(props)
        return inst_mod.InstanceDraft(
            profile_id=TEST_PROFILE,
            entity_type="Parameter",
            name=name,
            uid=registry.uid_for("Parameter", name, 0),
            props=base,
            evidence=[{"type": "manual", "note": "测试"}],
        )

    async def test_upsert_is_idempotent(self, session, registry):
        draft = self._draft(registry)
        first = await inst_mod.upsert_instances(session, [draft])
        await session.commit()
        assert (first.created, first.updated, first.unchanged) == (1, 0, 0)

        second = await inst_mod.upsert_instances(session, [draft])
        await session.commit()
        assert (second.created, second.updated, second.unchanged) == (0, 0, 1)

        third = await inst_mod.upsert_instances(session, [self._draft(registry, unit="cm")])
        await session.commit()
        assert (third.created, third.updated, third.unchanged) == (0, 1, 0)

    async def test_get_and_list(self, session, registry):
        await inst_mod.upsert_instances(session, [self._draft(registry, "a1"), self._draft(registry, "a2")])
        await session.commit()
        one = await inst_mod.get_instance(session, TEST_PROFILE, "Parameter", "a1")
        assert one is not None and one.props["group"] == "水源划分"
        rows = await inst_mod.list_instances(session, profile_id=TEST_PROFILE)
        assert {r.name for r in rows} == {"a1", "a2"}

    async def test_sync_to_graph_and_node_shape(self, session, registry):
        await inst_mod.upsert_instances(session, [self._draft(registry)])
        await session.commit()
        rows = await inst_mod.list_instances(session, profile_id=TEST_PROFILE)
        store = InMemoryGraphStore()
        count = await inst_mod.sync_to_graph(rows, store)
        assert count == 1
        node = await store.get_node(rows[0].uid)
        assert node.name == "sm"
        assert node.props["evidence_count"] == 1
        assert node.aliases == ["sm"]

    async def test_sync_to_graph_reports_failure_honestly(self, session, registry):
        """图库写失败时不能谎报成功（写入是静默降级的）。"""
        await inst_mod.upsert_instances(session, [self._draft(registry)])
        await session.commit()
        rows = await inst_mod.list_instances(session, profile_id=TEST_PROFILE)
        store = InMemoryGraphStore()

        async def _noop_upsert(nodes, edges):
            return None

        store.upsert = _noop_upsert  # type: ignore[method-assign]
        assert await inst_mod.sync_to_graph(rows, store) == 0
