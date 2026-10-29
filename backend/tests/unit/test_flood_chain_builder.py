"""防洪链台账 → 图节点/关系（纯函数）测试。

覆盖：字段解析、节点生成、关系方向、端点合法性、告警不中断。
"""
from __future__ import annotations

from app.services.graph.flood_chain import (
    build_flood_chain,
    parse_bool,
    parse_number,
    summarize,
)
from app.services.graph.schema import EntityType, ExtractionResult, RelationType

KB_ID = 1

PROJECTS = [
    {
        "工程名称": "鲤鱼塘水库", "工程类型": "reservoir", "管理单位": "XX县水利局",
        "所在乡镇": "XX镇", "坝高(m)": "58.5", "总库容(万m³)": "1200",
    },
    {
        "工程名称": "鲤鱼塘水库大坝", "工程类型": "dam",
        "所属水库": "鲤鱼塘水库", "坝高(m)": "58.5",
    },
]

CONDITIONS = [
    {
        "工况名": "设计工况", "工况类型": "design", "频率": "100年一遇",
        "库水位(m)": "412.30", "下泄流量(m³/s)": "1230", "依据条款": "调度规程 4.2",
    },
]

REACHES = [
    {"河段名": "下游河段1", "所属水库": "鲤鱼塘水库", "安全泄量(m³/s)": "800"},
    {"河段名": "下游河段2", "所属水库": "鲤鱼塘水库", "安全泄量(m³/s)": "600"},
]

IMPACTS = [
    {
        "工况": "设计工况", "河段": "下游河段1", "乡镇": "XX镇", "村": "A村",
        "是否预警": "是", "预警阈值": "800 m³/s", "是否转移": "是",
        "安置点": "中心小学", "转移路线": "沿河路", "依据条款": "预案 5.3",
        "责任人": "XX镇防汛责任人",
    },
    {
        "工况": "设计工况", "河段": "下游河段1", "乡镇": "XX镇", "村": "B村",
        "是否预警": "是", "预警阈值": "800 m³/s", "是否转移": "否",
    },
]


def _build() -> ExtractionResult:
    return build_flood_chain(KB_ID, PROJECTS, CONDITIONS, REACHES, IMPACTS)


def _names(result: ExtractionResult) -> dict[str, str]:
    return {n.uid: n.name for n in result.nodes}


def _has_edge(result: ExtractionResult, subject: str, predicate: RelationType, obj: str) -> bool:
    names = _names(result)
    for edge in result.edges:
        if (
            names.get(edge.subject_uid) == subject
            and edge.predicate == predicate
            and names.get(edge.object_uid) == obj
        ):
            return True
    return False


class TestParsing:
    def test_parse_number(self):
        assert parse_number("1230") == 1230.0
        assert parse_number("1,230 m³/s") == 1230.0
        assert parse_number("100年一遇") == 100.0
        assert parse_number("") is None
        assert parse_number(None) is None
        assert parse_number("无") is None

    def test_parse_bool(self):
        for truthy in ("是", "y", "√", "1", True):
            assert parse_bool(truthy) is True
        for falsy in ("否", "", None, False, "没有"):
            assert parse_bool(falsy) is False


class TestNodes:
    def test_project_and_building(self):
        result = _build()
        names = {n.name for n in result.nodes}
        assert "鲤鱼塘水库" in names
        assert "鲤鱼塘水库大坝" in names

    def test_condition_props(self):
        result = _build()
        cond = next(n for n in result.nodes if n.entity_type == EntityType.CONDITION)
        assert cond.props["discharge_m3s"] == 1230.0
        assert cond.props["reservoir_level_m"] == 412.3
        assert cond.props["frequency"] == "100年一遇"

    def test_reach_safe_discharge(self):
        """安全泄量按河段分别记录（用户确认的口径）。"""
        result = _build()
        reach1 = next(n for n in result.nodes if n.name == "下游河段1")
        reach2 = next(n for n in result.nodes if n.name == "下游河段2")
        assert reach1.props["safe_discharge_m3s"] == 800.0
        assert reach2.props["safe_discharge_m3s"] == 600.0

    def test_measure_nodes(self):
        result = _build()
        warnings = [n for n in result.nodes if n.entity_type == EntityType.MEASURE and n.props.get("measure_type") == "warning"]
        evacuations = [n for n in result.nodes if n.entity_type == EntityType.MEASURE and n.props.get("measure_type") == "evacuation"]
        assert len(warnings) == 2  # A村、B村
        assert len(evacuations) == 1  # 只有 A村要转移
        assert evacuations[0].props["shelter"] == "中心小学"


class TestRelations:
    def test_building_part_of_reservoir(self):
        assert _has_edge(_build(), "鲤鱼塘水库大坝", RelationType.PART_OF, "鲤鱼塘水库")

    def test_reservoir_located_in_town(self):
        assert _has_edge(_build(), "鲤鱼塘水库", RelationType.LOCATED_IN, "XX镇")

    def test_reach_downstream_of_reservoir(self):
        assert _has_edge(_build(), "鲤鱼塘水库", RelationType.DOWNSTREAM_OF, "下游河段1")

    def test_village_located_in_town(self):
        assert _has_edge(_build(), "A村", RelationType.LOCATED_IN, "XX镇")

    def test_reach_downstream_of_village(self):
        assert _has_edge(_build(), "下游河段1", RelationType.DOWNSTREAM_OF, "A村")

    def test_warning_and_evacuation(self):
        result = _build()
        assert _has_edge(result, "A村", RelationType.NEEDS_WARNING, "XX镇A村·预警发布")
        assert _has_edge(result, "A村", RelationType.NEEDS_EVACUATION, "XX镇A村·转移安置")
        # B村只预警不转移
        assert _has_edge(result, "B村", RelationType.NEEDS_WARNING, "XX镇B村·预警发布")
        assert not any(
            e.predicate == RelationType.NEEDS_EVACUATION
            for e in result.edges
            if _names(result).get(e.subject_uid) == "B村"
        )

    def test_measure_responsible(self):
        result = _build()
        names = _names(result)
        targets = [
            names[e.object_uid]
            for e in result.edges
            if e.predicate == RelationType.RESPONSIBLE
        ]
        assert "XX镇防汛责任人" in targets

    def test_all_edges_respect_endpoints(self):
        """生成的关系必须全部通过端点校验（否则说明 schema 与构建器不一致）。"""
        from app.services.graph.schema import endpoints_allowed

        result = _build()
        type_by_uid = {n.uid: n.entity_type for n in result.nodes}
        for edge in result.edges:
            assert endpoints_allowed(
                edge.predicate, type_by_uid[edge.subject_uid], type_by_uid[edge.object_uid]
            ), f"端点非法: {edge.to_dict()}"


class TestWarnings:
    def test_unknown_reach_warns_but_continues(self):
        impacts = IMPACTS + [
            {"工况": "设计工况", "河段": "不存在的河段", "乡镇": "YY镇", "是否预警": "否"},
        ]
        result = build_flood_chain(KB_ID, PROJECTS, CONDITIONS, REACHES, impacts)
        assert any("不存在的河段" in w for w in result.warnings)
        # 后面的行仍然被处理
        assert any(n.name == "YY镇" for n in result.nodes)

    def test_missing_town_skipped(self):
        result = build_flood_chain(KB_ID, PROJECTS, CONDITIONS, REACHES, [{"工况": "设计工况"}])
        assert any("缺少「乡镇」" in w for w in result.warnings)

    def test_summary(self):
        summary = summarize(_build())
        assert summary["nodes"] > 0 and summary["edges"] > 0
        assert "by_entity_type" in summary and "by_relation_type" in summary
