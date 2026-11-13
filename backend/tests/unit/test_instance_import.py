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
