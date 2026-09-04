"""Excel/CSV 解析器单元测试：日期列规范化（单元二 子单元③）+ 表格结构就绪（2-1）。"""
from __future__ import annotations

import pytest

from app.services.parser.excel_parser import (
    ExcelParser,
    _normalize_excel_cell,
    _normalize_excel_row,
)
from app.services.parser.ir import ElementType
from app.services.parser.ir_validation import validate_elements


class TestNormalizeExcelCell:
    def test_iso_datetime_string_strips_time(self):
        """pandas dtype=str 读 datetime 列 → "2025-09-01 00:00:00" → "2025-09-01"。"""
        assert _normalize_excel_cell("2025-09-01 00:00:00") == "2025-09-01"

    def test_dotted_date_normalized_to_iso(self):
        """文本点分日期 "2025.9.15" → "2025-09-15"（可排序、可精确筛选）。"""
        assert _normalize_excel_cell("2025.9.15") == "2025-09-15"
        assert _normalize_excel_cell("2025.9.5") == "2025-09-05"  # 补零

    def test_plain_iso_date_kept(self):
        assert _normalize_excel_cell("2025-09-15") == "2025-09-15"

    def test_non_date_untouched(self):
        assert _normalize_excel_cell("质量保证体系") == "质量保证体系"
        assert _normalize_excel_cell("/") == "/"
        assert _normalize_excel_cell("001") == "001"

    def test_none_becomes_empty(self):
        assert _normalize_excel_cell(None) == ""

    def test_timestamp_object(self):
        """pandas Timestamp 对象 → ISO 日期。"""
        import pandas as pd

        ts = pd.Timestamp("2025-09-23")
        assert _normalize_excel_cell(ts) == "2025-09-23"


class TestNormalizeExcelRow:
    def test_row_dates_unified(self):
        """同一行里混合点分日期与带时刻日期 → 全部统一 YYYY-MM-DD。"""
        row = ["1", "施工方案", "2025.9.15", "2025-09-16 00:00:00", "已报送"]
        assert _normalize_excel_row(row) == [
            "1", "施工方案", "2025-09-15", "2025-09-16", "已报送",
        ]


class TestExcelElementTable:
    """单元二 2-1：Excel 解析产出 element.table（与 MinerU 的 {rows, header_path} 一致）。"""

    def test_excel_element_carries_table_structure(self, tmp_path):
        """一个 sheet → 一个 TABLE element，携带完整列名 + 全部行（日期已规范化）。"""
        pytest.importorskip("openpyxl")
        import pandas as pd

        p = tmp_path / "方案台账.xlsx"
        frame = pd.DataFrame(
            {
                "序号": ["1", "2"],
                "方案名称": ["质量保证体系", "施工组织设计"],
                "完成时间": ["2025.9.15", "2025-09-01 00:00:00"],
            }
        )
        frame.to_excel(str(p), index=False)

        parsed = ExcelParser().parse(p, "方案台账.xlsx")

        assert len(parsed.elements) == 1, "一个 sheet 应产出 1 个 TABLE element"
        el = parsed.elements[0]
        assert el.type == ElementType.TABLE
        assert el.table is not None, "element.table 应已填充（不再拍扁成纯文本）"
        # 列名与源文件一致（header_path = 列名）
        assert el.table["header_path"] == ["序号", "方案名称", "完成时间"]
        # rows = 表头 + 数据行；日期统一 YYYY-MM-DD
        assert el.table["rows"] == [
            ["序号", "方案名称", "完成时间"],
            ["1", "质量保证体系", "2025-09-15"],
            ["2", "施工组织设计", "2025-09-01"],
        ]
        # IR 校验通过（每行列数 = 表头列数）
        assert validate_elements(parsed.elements) == []

    def test_multi_sheet_one_element_each(self, tmp_path):
        """多 sheet 各产出独立 TABLE element，列名各自与源一致。"""
        pytest.importorskip("openpyxl")
        import pandas as pd

        p = tmp_path / "多表.xlsx"
        with pd.ExcelWriter(str(p), engine="openpyxl") as w:
            pd.DataFrame({"设备": ["配电箱"], "数量": ["3"]}).to_excel(w, sheet_name="设备台账", index=False)
            pd.DataFrame({"方案": ["A"], "状态": ["已完成"]}).to_excel(w, sheet_name="方案清单", index=False)

        parsed = ExcelParser().parse(p, "多表.xlsx")

        assert len(parsed.elements) == 2, "两个 sheet 应产出 2 个 TABLE element"
        headers = [el.table["header_path"] for el in parsed.elements]
        assert ["设备", "数量"] in headers
        assert ["方案", "状态"] in headers
