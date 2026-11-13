"""F3-3：从 Excel 表头推断属性（只预览，不写库）。

规则很保守，因为它给的是**建议**：宁可推成 string 让用户改，
也不要猜成 enum/number 把数据框死。
"""
from __future__ import annotations

import io

import pytest

from app.services.graph.ontology import excel_schema


def _xlsx(headers, rows) -> io.BytesIO:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class TestSuggestColumns:
    def test_numeric_column_becomes_number(self):
        cols = excel_schema.suggest_columns(
            _xlsx(["safe_discharge", "名称"], [["1250", "青溪水库"], ["980", "白水河"]])
        )
        first = cols[0]
        assert first.name == "safe_discharge"
        assert first.type == "number"

    def test_few_short_values_become_enum(self):
        cols = excel_schema.suggest_columns(
            _xlsx(["级别"], [["高"], ["中"], ["高"], ["低"]])
        )
        assert cols[0].type == "enum"
        assert cols[0].values == ["高", "中", "低"], "去重且保持出现顺序"

    def test_long_text_stays_string(self):
        long_text = "这是一段很长的说明文字，用来确认推断不会把自由文本塞成枚举值"
        cols = excel_schema.suggest_columns(_xlsx(["备注"], [[long_text]]))
        assert cols[0].type == "string"
        assert cols[0].values == []

    def test_non_ascii_header_gets_placeholder_and_keeps_chinese_in_description(self):
        cols = excel_schema.suggest_columns(_xlsx(["安全泄量"], [["1250"]]))
        assert cols[0].name == "col_1", "中文列名转不成标识，先占位，由用户在界面上改"
        assert cols[0].description == "安全泄量"

    def test_ascii_header_with_unit_keeps_identifier(self):
        cols = excel_schema.suggest_columns(_xlsx(["safe_discharge(m3/s)"], [["1250"]]))
        assert cols[0].name == "safe_discharge"

    def test_empty_sheet(self):
        assert excel_schema.suggest_columns(_xlsx([], [])) == []

    def test_blank_header_columns_are_skipped(self):
        cols = excel_schema.suggest_columns(_xlsx(["名称", None, ""], [["水库", "x", "y"]]))
        assert len(cols) == 1, "空表头列不该生成属性"


class TestParseYamlProfile:
    def test_valid(self):
        meta = excel_schema.parse_yaml_profile(
            "profile_id: demo\ndisplay_name: 演示\nentity_types:\n  - name: A\n"
        )
        assert meta["profile_id"] == "demo"
        assert meta["display_name"] == "演示"

    def test_missing_profile_id(self):
        from app.services.graph.ontology.registry import ProfileError

        with pytest.raises(ProfileError, match="profile_id"):
            excel_schema.parse_yaml_profile("entity_types:\n  - name: A\n")

    def test_missing_entity_types(self):
        from app.services.graph.ontology.registry import ProfileError

        with pytest.raises(ProfileError, match="entity_types"):
            excel_schema.parse_yaml_profile("profile_id: demo\n")

    def test_broken_yaml(self):
        from app.services.graph.ontology.registry import ProfileError

        with pytest.raises(ProfileError, match="YAML 解析失败"):
            excel_schema.parse_yaml_profile("profile_id: [unclosed\n")
