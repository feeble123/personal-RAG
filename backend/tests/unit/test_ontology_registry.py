"""通用本体引擎（TBox）：YAML 加载、校验、缓存、注册。

关键门禁：
- 引擎代码不得出现具体模型名（模型知识只能是数据）
- 防洪链的枚举行为不受影响
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.graph import schema as graph_schema
from app.services.graph.ontology import (
    PROFILES_DIR,
    ProfileError,
    get_registry,
    load_profile,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_cache()
    yield
    reset_cache()


class TestLoadDemoProfile:
    def test_structure(self):
        reg = load_profile("tuning")
        assert reg.profile_id == "tuning"
        assert reg.domain == "model_tuning"
        # 调参域：6 类实体 / 6 种关系（流程骨架在 process_skeleton.yaml）
        assert len(reg.entity_types) == 6
        assert len(reg.relation_types) == 6

    def test_entity_definition(self):
        reg = load_profile("tuning")
        param = reg.entity("Parameter")
        assert param is not None
        assert param.display_name == "参数"
        assert param.scope == "global"
        assert "group" in param.attributes
        assert param.attributes["group"].required is True
        assert param.attributes["group"].values == ("蒸散发", "产流", "水源划分", "汇流", "其他")

    def test_endpoints(self):
        reg = load_profile("tuning")
        assert reg.endpoints_ok("PART_OF", "Parameter", "Parameter")
        assert reg.endpoints_ok("AFFECTS", "Parameter", "Metric")
        assert not reg.endpoints_ok("AFFECTS", "Metric", "Parameter")
        assert not reg.endpoints_ok("NOT_EXIST", "Parameter", "Metric")

    def test_uid_scope(self):
        reg = load_profile("tuning")
        p1 = reg.uid_for("Parameter", "sm", kb_id=1)
        p2 = reg.uid_for("Parameter", "sm", kb_id=2)
        assert p1 == p2, "global 类型的 uid 不应随知识库变化"

        s1 = reg.uid_for("Strategy", "分组率定", kb_id=1)
        s2 = reg.uid_for("Strategy", "分组率定", kb_id=2)
        assert s1 != s2, "local 类型的 uid 必须按库隔离"

    def test_unknown_type_raises(self):
        reg = load_profile("tuning")
        with pytest.raises(ProfileError, match="未定义实体类型"):
            reg.uid_for("NotAType", "x")


class TestRegistration:
    def test_dynamic_types_registered_in_schema(self):
        load_profile("tuning")
        node = graph_schema.Node(uid="pn:1", entity_type="Parameter", name="sm")
        assert node.to_dict()["entity_type"] == "Parameter"
        assert node.label == "Parameter"

    def test_schema_endpoints_use_dynamic_table(self):
        load_profile("tuning")
        assert graph_schema.endpoints_allowed("AFFECTS", "Parameter", "Metric")
        assert not graph_schema.endpoints_allowed("AFFECTS", "Metric", "Parameter")

    def test_flood_chain_enum_still_works(self):
        """动态注册不能影响防洪链枚举。"""
        load_profile("tuning")
        assert graph_schema.endpoints_allowed(
            graph_schema.RelationType.PART_OF,
            graph_schema.EntityType.ENGINEERING_OBJECT,
            graph_schema.EntityType.ENGINEERING_OBJECT,
        )
        node = graph_schema.Node(
            uid="eo:1", entity_type=graph_schema.EntityType.ENGINEERING_OBJECT, name="某水库"
        )
        assert node.label == "EngineeringObject"
        assert node.to_dict()["entity_type"] == "engineering_object"


class TestProcessSkeleton:
    def test_structure(self):
        reg = load_profile("process_skeleton")
        assert set(reg.entity_types) == {"Stage", "Action", "Tool", "DataAsset"}
        assert set(reg.relation_types) == {"NEXT_STAGE", "PERFORMED_BY", "USES_TOOL", "REQUIRES_DATA"}

    def test_endpoints(self):
        reg = load_profile("process_skeleton")
        assert reg.endpoints_ok("NEXT_STAGE", "Stage", "Stage")
        assert reg.endpoints_ok("PERFORMED_BY", "Stage", "Action")
        assert not reg.endpoints_ok("PERFORMED_BY", "Action", "Stage")

    def test_two_profiles_coexist(self):
        """调参本体与流程骨架同时注册，类型互不覆盖（uid 前缀不冲突）。"""
        load_profile("tuning")
        load_profile("process_skeleton")
        assert graph_schema.uid_for("Parameter", "sm", 0) != graph_schema.uid_for("Stage", "数据下载", 0)
        node = graph_schema.Node(uid="st:1", entity_type="Stage", name="数据下载")
        assert node.label == "Stage"
        assert graph_schema.endpoints_allowed("AFFECTS", "Parameter", "Metric")
        assert graph_schema.endpoints_allowed("PERFORMED_BY", "Stage", "Action")


class TestCache:
    def test_cached_instance(self):
        first = get_registry("tuning")
        second = get_registry("tuning")
        assert first is second

    def test_reload_returns_new_instance(self):
        first = get_registry("tuning")
        second = get_registry("tuning", reload=True)
        assert second is not first
        assert second.profile_id == first.profile_id


class TestInvalidProfiles:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "bad.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_entity_prefix(self, tmp_path):
        path = self._write(
            tmp_path,
            """
version: 1
profile_id: bad
entity_types:
  - name: Thing
    attributes: []
relation_types: []
""",
        )
        with pytest.raises(ProfileError, match="uid_prefix"):
            load_profile(path)

    def test_relation_points_to_unknown_type(self, tmp_path):
        path = self._write(
            tmp_path,
            """
version: 1
profile_id: bad
entity_types:
  - name: Thing
    uid_prefix: th
    attributes: []
relation_types:
  - { name: USES, source: Thing, target: Ghost }
""",
        )
        with pytest.raises(ProfileError, match="未定义的实体类型"):
            load_profile(path)

    def test_enum_without_values(self, tmp_path):
        path = self._write(
            tmp_path,
            """
version: 1
profile_id: bad
entity_types:
  - name: Thing
    uid_prefix: th
    attributes:
      - { name: color, type: enum }
relation_types: []
""",
        )
        with pytest.raises(ProfileError, match="enum"):
            load_profile(path)

    def test_broken_yaml(self, tmp_path):
        path = self._write(tmp_path, "version: 1\n  bad: [")
        with pytest.raises(ProfileError, match="YAML"):
            load_profile(path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ProfileError, match="不存在"):
            load_profile(tmp_path / "nope.yaml")


class TestExtends:
    def test_child_inherits_parent(self, tmp_path):
        base = tmp_path / "base.yaml"
        base.write_text(
            """
version: 1
profile_id: base
entity_types:
  - name: Node
    uid_prefix: nd
    attributes:
      - { name: label, type: string }
relation_types:
  - { name: LINKS, source: Node, target: Node }
""",
            encoding="utf-8",
        )
        child = tmp_path / "child.yaml"
        child.write_text(
            f"""
version: 1
extends: {base.as_posix()}
profile_id: child
entity_types:
  - name: Extra
    uid_prefix: ex
    attributes: []
relation_types:
  - {{ name: BELONGS, source: Extra, target: Node }}
""",
            encoding="utf-8",
        )
        reg = load_profile(child)
        assert {"Node", "Extra"} <= set(reg.entity_types)
        assert {"LINKS", "BELONGS"} <= set(reg.relation_types)
        assert reg.endpoints_ok("BELONGS", "Extra", "Node")


class TestNoModelNamesInEngine:
    def test_engine_sources_have_no_model_names(self):
        """引擎代码里不允许出现具体模型名（模型知识只能是 profiles 数据）。"""
        ontology_dir = PROFILES_DIR.parent
        offenders = []
        for path in ontology_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for token in ("xaj", "swat", "topmodel", "tank model"):
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        assert not offenders, f"引擎代码出现了模型名：{offenders}"
