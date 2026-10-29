"""台账模板生成与读取：保证「模板列名」与「导入解析」永远一致。"""
from __future__ import annotations

import pytest
from openpyxl import load_workbook

from app.services.graph.template_io import (
    REQUIRED_SHEETS,
    SHEETS,
    build_workbook,
    read_template,
    write_template,
)


class TestBuild:
    def test_sheets_present(self):
        wb = build_workbook()
        assert "说明" in wb.sheetnames
        assert "填写示例" in wb.sheetnames
        for name in REQUIRED_SHEETS:
            assert name in wb.sheetnames

    def test_headers_match_definition(self):
        wb = build_workbook()
        for name, headers in SHEETS.items():
            ws = wb[name]
            actual = [c.value for c in ws[1]]
            assert actual == headers, f"{name} 表头与定义不一致"

    def test_data_sheets_are_empty(self):
        """正式表只留表头，示例放在单独的「填写示例」工作表，避免误导入。"""
        wb = build_workbook()
        for name in REQUIRED_SHEETS:
            assert wb[name].max_row == 1


class TestRead:
    def test_empty_template(self, tmp_path):
        path = write_template(tmp_path / "t.xlsx")
        sheets = read_template(path)
        assert set(sheets) == {"projects", "conditions", "reaches", "impacts"}
        assert all(rows == [] for rows in sheets.values())

    def test_reads_rows(self, tmp_path):
        wb = build_workbook()
        wb["工程表"].append(["鲤鱼塘水库", "reservoir", "", "XX县水利局", "XX镇", "58.5", "1200", ""])
        wb["工况表"].append(["设计工况", "design", "100年一遇", "412.3", "1230", "调度规程 4.2", "", ""])
        path = tmp_path / "filled.xlsx"
        wb.save(path)

        sheets = read_template(path)
        assert sheets["projects"][0]["工程名称"] == "鲤鱼塘水库"
        assert sheets["projects"][0]["坝高(m)"] == "58.5"
        assert sheets["conditions"][0]["下泄流量(m³/s)"] == "1230"

    def test_skips_blank_and_sample_rows(self, tmp_path):
        wb = build_workbook()
        ws = wb["河段表"]
        ws.append(["下游河段1", "鲤鱼塘水库", "坝下", "XX桥", "6.5", "800", "附表3", ""])
        ws.append([None, None, None, None, None, None, None, None])
        ws.append(["示例河段", "", "", "", "", "", "", "示例行"])
        path = tmp_path / "mixed.xlsx"
        wb.save(path)

        rows = read_template(path)["reaches"]
        assert len(rows) == 1
        assert rows[0]["河段名"] == "下游河段1"

    def test_missing_sheet_raises(self, tmp_path):
        path = tmp_path / "broken.xlsx"
        load_workbook_from_scratch(path)
        with pytest.raises(ValueError, match="缺少工作表"):
            read_template(path)


def load_workbook_from_scratch(path):
    """造一个只有默认工作表的坏文件。"""
    from openpyxl import Workbook

    wb = Workbook()
    wb.save(path)
