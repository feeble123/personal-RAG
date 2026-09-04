"""Excel/CSV 解析器单元测试：日期列规范化（单元二 子单元③）。"""
from __future__ import annotations

from app.services.parser.excel_parser import (
    _normalize_excel_cell,
    _normalize_excel_row,
)


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
