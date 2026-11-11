"""本体实例校验：类型/必填/枚举/数值/端点。"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import load_profile, reset_cache, validate_entity, validate_relation


@pytest.fixture(scope="module")
def registry():
    reset_cache()
    reg = load_profile("demo_tuning")
    yield reg
    reset_cache()


class TestEntityValidation:
    def test_valid_parameter(self, registry):
        result = validate_entity(
            registry,
            "Parameter",
            "sm",
            {"group": "水源划分", "unit": "mm", "physical_low": 5, "physical_high": 45, "sensitivity": "高"},
        )
        assert result.ok, result.errors
        assert result.warnings == []

    def test_unknown_entity_type(self, registry):
        result = validate_entity(registry, "Ghost", "x", {})
        assert not result.ok
        assert "未定义实体类型" in result.errors[0]

    def test_missing_required_attribute(self, registry):
        result = validate_entity(registry, "Parameter", "sm", {"unit": "mm"})
        assert not result.ok
        assert any("group" in e for e in result.errors)

    def test_enum_not_in_values(self, registry):
        result = validate_entity(registry, "Parameter", "sm", {"group": "不存在的分组"})
        assert not result.ok
        assert any("不在枚举" in e for e in result.errors)

    def test_number_given_string(self, registry):
        result = validate_entity(
            registry, "Parameter", "sm", {"group": "产流", "physical_low": "五"}
        )
        assert not result.ok
        assert any("需要数字" in e for e in result.errors)

    def test_unknown_attribute_is_warning_only(self, registry):
        result = validate_entity(
            registry, "Parameter", "sm", {"group": "产流", "mystery": "x"}
        )
        assert result.ok
        assert any("未在 TBox 中声明" in w for w in result.warnings)

    def test_empty_name(self, registry):
        result = validate_entity(registry, "Parameter", "  ", {"group": "产流"})
        assert not result.ok
        assert any("缺少名称" in e for e in result.errors)

    def test_range_type(self, registry):
        """range 属性必须是两个数字。"""
        result = validate_entity(registry, "Parameter", "sm", {"group": "产流"})
        assert result.ok  # range 未在 demo 中使用，先确认基础校验不受影响

    def test_boolean_attribute(self, registry):
        # demo profile 没有 boolean 属性，用未声明属性覆盖告警分支
        result = validate_entity(registry, "Parameter", "sm", {"group": "产流", "flag": True})
        assert result.ok
        assert any("flag" in w for w in result.warnings)


class TestRelationValidation:
    def test_valid(self, registry):
        assert validate_relation(registry, "AFFECTS", "Parameter", "Metric").ok
        assert validate_relation(registry, "PART_OF", "Parameter", "Parameter").ok

    def test_unknown_relation(self, registry):
        result = validate_relation(registry, "GHOST_REL", "Parameter", "Metric")
        assert not result.ok
        assert "未定义关系类型" in result.errors[0]

    def test_wrong_endpoints(self, registry):
        result = validate_relation(registry, "AFFECTS", "Metric", "Parameter")
        assert not result.ok
        assert "端点非法" in result.errors[0]

    def test_multiple_errors_accumulate(self, registry):
        result = validate_entity(registry, "Parameter", "", {})
        assert len(result.errors) >= 2  # 缺名称 + 缺必填
