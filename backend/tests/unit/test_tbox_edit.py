"""F3：TBox 在线编辑（写回 YAML）。

要点：
- 改动真的落到 YAML 里，而且**注释与顺序不丢**（ruamel round-trip）
- 新类型/新属性建得出来，改中文名能改，改建 name / uid_prefix 直接拒绝（会让老实例孤儿）
- 删除有保护的：被关系引用的类型、必填属性、有实例的类型
- 写入要么全对要么不动：校验失败时文件必须恢复原状
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import libraries as lib_mod
from app.services.graph.ontology import registry as reg_mod
from app.services.graph.ontology import tbox_edit
from app.services.graph.ontology.registry import ProfileError


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """真实 TBox 复制到临时目录，测试不碰仓库里的文件。"""
    src = reg_mod.PROFILES_DIR / "flood_chain.yaml"
    (tmp_path / "flood_chain.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lib_mod, "PROFILES_DIR", tmp_path)
    monkeypatch.setattr(reg_mod, "PROFILES_DIR", tmp_path)
    reg_mod.reset_cache()
    yield "flood_chain", tmp_path
    reg_mod.reset_cache()


class TestEntityType:
    def test_create_entity_type_persists_to_yaml(self, profile):
        pid, path = profile
        result = tbox_edit.upsert_entity_type(
            pid, tbox_edit.EntityTypeInput("warning_zone", "预警区", "wz", "local")
        )
        text = (path / "flood_chain.yaml").read_text(encoding="utf-8")

        assert "name: warning_zone" in text
        assert "display_name: 预警区" in text
        # 注释是 TBox 的设计说明，编辑不能弄丢
        assert "防洪链本体（TBox）" in text
        assert any(t["name"] == "warning_zone" for t in result["entity_types"])

    def test_update_display_name(self, profile):
        pid, path = profile
        tbox_edit.upsert_entity_type(
            pid, tbox_edit.EntityTypeInput("river", "河道（改名）", "rv", "global")
        )
        text = (path / "flood_chain.yaml").read_text(encoding="utf-8")
        assert "河道（改名）" in text
        assert text.count("name: river") == 1, "更新不能变成新增一条"
        assert "name: river" in text

    def test_uid_prefix_is_immutable(self, profile):
        pid, _ = profile
        with pytest.raises(ProfileError, match="编号前缀不允许修改"):
            tbox_edit.upsert_entity_type(
                pid, tbox_edit.EntityTypeInput("river", "河流河段", "xx", "global")
            )

    def test_delete_blocked_when_referenced_by_relation(self, profile):
        pid, _ = profile
        with pytest.raises(ProfileError, match="被关系类型"):
            tbox_edit.delete_entity_type(pid, "river")

    def test_delete_blocked_when_has_instances(self, profile):
        pid, _ = profile
        with pytest.raises(ProfileError, match="还有 3 个实例"):
            tbox_edit.delete_entity_type(pid, "measure", instance_count=3)

    def test_delete_unused_type(self, profile):
        pid, path = profile
        tbox_edit.upsert_entity_type(
            pid, tbox_edit.EntityTypeInput("temp_type", "临时类型", "tt", "local")
        )
        tbox_edit.delete_entity_type(pid, "temp_type")
        assert "name: temp_type" not in (path / "flood_chain.yaml").read_text(encoding="utf-8")


class TestAttribute:
    def test_add_attribute(self, profile):
        pid, path = profile
        result = tbox_edit.upsert_attribute(
            pid,
            "river",
            tbox_edit.AttributeInput("flood_warning_level", "enum", ("红", "橙", "黄")),
        )
        text = (path / "flood_chain.yaml").read_text(encoding="utf-8")
        assert "flood_warning_level" in text
        river = next(t for t in result["entity_types"] if t["name"] == "river")
        assert any(a["name"] == "flood_warning_level" for a in river["attributes"])

    def test_enum_requires_values(self, profile):
        pid, _ = profile
        with pytest.raises(ProfileError, match="枚举类型必须给出"):
            tbox_edit.upsert_attribute(pid, "river", tbox_edit.AttributeInput("x", "enum"))

    def test_delete_required_attribute_is_blocked(self, profile):
        pid, path = profile
        tbox_edit.upsert_attribute(
            pid, "river", tbox_edit.AttributeInput("must_fill", "string", required=True)
        )
        with pytest.raises(ProfileError, match="必填"):
            tbox_edit.delete_attribute(pid, "river", "must_fill")

    def test_delete_optional_attribute(self, profile):
        pid, path = profile
        tbox_edit.upsert_attribute(pid, "river", tbox_edit.AttributeInput("temp_attr", "string"))
        tbox_edit.delete_attribute(pid, "river", "temp_attr")
        assert "temp_attr" not in (path / "flood_chain.yaml").read_text(encoding="utf-8")


class TestAtomicWrite:
    def test_invalid_change_rolls_back(self, tmp_path, monkeypatch):
        """删掉唯一一个实体类型 → 校验失败（列表不能为空）→ 文件必须原样恢复。"""
        text = """version: 1
profile_id: tiny
display_name: 小库
domain: test
entity_types:
  - name: Thing
    display_name: 东西
    uid_prefix: tg
    scope: local
relation_types: []
"""
        (tmp_path / "tiny.yaml").write_text(text, encoding="utf-8")
        monkeypatch.setattr(lib_mod, "PROFILES_DIR", tmp_path)
        monkeypatch.setattr(reg_mod, "PROFILES_DIR", tmp_path)
        reg_mod.reset_cache()
        try:
            with pytest.raises(ProfileError, match="已回滚"):
                tbox_edit.delete_entity_type("tiny", "Thing")
            assert (tmp_path / "tiny.yaml").read_text(encoding="utf-8") == text
        finally:
            reg_mod.reset_cache()


class TestRelationType:
    def test_create_relation_type(self, profile):
        pid, path = profile
        result = tbox_edit.upsert_relation_type(
            pid,
            tbox_edit.RelationTypeInput(
                "WARNS", "告警", "admin_region", "measure",
                target_types=("measure",), description="行政区触发措施",
            ),
        )
        text = (path / "flood_chain.yaml").read_text(encoding="utf-8")
        assert "name: WARNS" in text
        assert "display_name: 告警" in text
        assert any(r["name"] == "WARNS" for r in result["relation_types"])

    def test_update_keeps_single_entry(self, profile):
        pid, path = profile
        tbox_edit.upsert_relation_type(
            pid, tbox_edit.RelationTypeInput("WARNS", "告警", "admin_region", "measure")
        )
        tbox_edit.upsert_relation_type(
            pid, tbox_edit.RelationTypeInput("WARNS", "预警提醒", "admin_region", "measure")
        )
        text = (path / "flood_chain.yaml").read_text(encoding="utf-8")
        assert text.count("name: WARNS") == 1
        assert "预警提醒" in text

    def test_unknown_entity_type_is_rejected(self, profile):
        pid, _ = profile
        with pytest.raises(ProfileError, match="未定义的实体类型"):
            tbox_edit.upsert_relation_type(
                pid, tbox_edit.RelationTypeInput("BAD_REL", "坏关系", "river", "no_such_type")
            )

    def test_delete_blocked_when_edges_exist(self, profile):
        pid, _ = profile
        with pytest.raises(ProfileError, match="已有 7 条关系在用"):
            tbox_edit.delete_relation_type(pid, "PART_OF", edge_count=7)

    def test_delete_relation_type(self, profile):
        pid, path = profile
        tbox_edit.upsert_relation_type(
            pid, tbox_edit.RelationTypeInput("TEMP_REL", "临时", "river", "river")
        )
        tbox_edit.delete_relation_type(pid, "TEMP_REL")
        assert "name: TEMP_REL" not in (path / "flood_chain.yaml").read_text(encoding="utf-8")
