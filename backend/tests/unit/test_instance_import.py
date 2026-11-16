"""实例 Excel 批量导入（F3-3）：表头对得上属性才认，对不上就明说。"""
from __future__ import annotations

import io

import pytest

from app.services.graph.ontology import instance_import
from app.services.graph.ontology.registry import AttributeDef, EntityTypeDef, OntologyRegistry


def _xlsx(headers, rows) -> io.BytesIO:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _registry() -> OntologyRegistry:
    gate = EntityTypeDef(
        name="gate", display_name="水闸", uid_prefix="gt", scope="local", label="Gate",
        attributes={
            "design_flow": AttributeDef(name="design_flow", type="number", required=True,
                                        description="设计流量"),
            "gate_type": AttributeDef(name="gate_type", type="enum", values=("平板闸", "弧形闸"),
                                      description="闸型"),
            "note": AttributeDef(name="note", type="text", description="备注"),
        },
    )
    return OntologyRegistry(profile_id="gate_dispatch", version=1, domain="test",
                            entity_types={"gate": gate}, relation_types={})


class TestPreviewImport:
    def test_maps_by_identifier_and_chinese_description(self):
        buf = _xlsx(["名称", "设计流量", "闸型"], [["青溪闸", "320", "平板闸"]])
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.total == 1 and not p.errors
        assert p.rows[0].name == "青溪闸"
        assert p.rows[0].props == {"design_flow": 320, "gate_type": "平板闸"}

    def test_unmatched_header_is_reported_not_guessed(self):
        buf = _xlsx(["名称", "设计流量", "随便什么"], [["青溪闸", "320", "x"]])
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.skipped_headers == ["随便什么"]
        assert "随便什么" not in p.mapped

    def test_missing_name_column_rejected(self):
        buf = _xlsx(["设计流量"], [["320"]])
        with pytest.raises(ValueError, match="名称列"):
            instance_import.preview_import(buf, _registry(), "gate")

    def test_missing_required_field_reports_row(self):
        buf = _xlsx(["名称", "闸型"], [["青溪闸", "平板闸"]])
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.rows == []
        assert "缺少必填字段" in p.errors[0]["message"]

    def test_non_numeric_in_number_column_reports_row(self):
        buf = _xlsx(["名称", "设计流量"], [["青溪闸", "很大"]])
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert "不是数字" in p.errors[0]["message"]

    def test_blank_rows_are_skipped(self):
        buf = _xlsx(["名称", "设计流量"], [["青溪闸", "320"], [None, None], ["", ""]])
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.total == 1

    def test_unknown_entity_type(self):
        buf = _xlsx(["名称"], [["x"]])
        with pytest.raises(ValueError, match="不存在"):
            instance_import.preview_import(buf, _registry(), "no_such_type")


class TestEvidenceColumns:
    """依据列：表里写了就带上，导入后由接口锚定成 doc_id/chunk_id。

    以前实例导入完全不认依据，导进去的实例天生没有出处，
    界面上再怎么点也只能看到一句手写的话。
    """

    def test_evidence_columns_are_parsed(self):
        buf = _xlsx(
            ["名称", "设计流量", "依据类型", "依据说明", "依据文档", "依据页码"],
            [["青溪闸", "320", "手册", "设计流量按 20 年一遇", "水闸设计规范.pdf", "12"]],
        )
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.skipped_headers == []  # 证据列不该被当成「认不出的表头」
        assert p.rows[0].evidence == [
            {
                "type": "手册",
                "note": "设计流量按 20 年一遇",
                "doc_name": "水闸设计规范.pdf",
                "page": 12,
            }
        ]

    def test_blank_evidence_cells_produce_no_evidence(self):
        buf = _xlsx(
            ["名称", "设计流量", "依据说明", "依据文档"],
            [["青溪闸", "320", None, ""]],
        )
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.rows[0].evidence == []

    def test_attribute_column_wins_over_evidence_header(self):
        """表头同时能对上属性时不抢：属性优先，证据列只是补充。"""
        buf = _xlsx(["名称", "设计流量", "note"], [["青溪闸", "320", "台账"]] )
        p = instance_import.preview_import(buf, _registry(), "gate")
        assert p.rows[0].props["note"] == "台账"
        assert p.rows[0].evidence == []
