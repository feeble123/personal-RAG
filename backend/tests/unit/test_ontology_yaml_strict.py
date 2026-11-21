"""严格 YAML 加载：重复键必须炸，不能静默覆盖。

背景：一条边写两段 `props`（一段属性、一段关系级属性）时，PyYAML 会让后者
吃掉前者——丢的是目标水位这种关键值，而且全链路没有任何提示。这里的用例
既是这个加载器的规格，也是 `profiles/` 下所有 YAML 的门禁。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.graph.ontology import PROFILES_DIR, ProfileError
from app.services.graph.ontology.yaml_strict import DuplicateKeyError, load_yaml_strict


class TestDuplicateKey:
    def test_同层重复键报错并给行号(self):
        text = "a:\n  x: 1\n  x: 2\n"
        with pytest.raises(DuplicateKeyError) as exc:
            load_yaml_strict(text)
        assert exc.value.key == "x"
        assert exc.value.line == 3
        assert exc.value.first_line == 2

    def test_重复键报错时带文件名(self):
        text = "props:\n  target_level_m: 340.0\nprops:\n  clause: x\n"
        with pytest.raises(DuplicateKeyError) as exc:
            load_yaml_strict(text, source="reservoir_flood.data.yaml")
        assert exc.value.key == "props"
        assert "reservoir_flood.data.yaml" in str(exc.value)

    def test_嵌套映射里的重复键也要报(self):
        text = "relations:\n  - subject:\n      name: a\n      name: b\n"
        with pytest.raises(DuplicateKeyError):
            load_yaml_strict(text)

    def test_不同映射里的同名键不算重复(self):
        text = "items:\n  - props:\n      a: 1\n  - props:\n      a: 2\n"
        data = load_yaml_strict(text)
        assert [item["props"]["a"] for item in data["items"]] == [1, 2]


class TestNormalLoading:
    def test_普通文档照常解析(self):
        data = load_yaml_strict("a: 1\nb:\n  - x: 1\n")
        assert data == {"a": 1, "b": [{"x": 1}]}

    def test_语法错误仍然抛_yaml_错误(self):
        with pytest.raises(yaml.YAMLError):
            load_yaml_strict("a: [1, 2\n")

    def test_空文件返回_none(self):
        assert load_yaml_strict("# 只有注释\n") is None


class TestProfileFiles:
    """`profiles/` 下的每一份 YAML 都必须无重复键——这是发布门禁。"""

    @pytest.mark.parametrize(
        "path", sorted(p for p in PROFILES_DIR.glob("*.yaml")), ids=lambda p: p.name
    )
    def test_文件没有重复键(self, path: Path):
        load_yaml_strict(path.read_text(encoding="utf-8"), source=path.name)

    def test_坏_profile_被_registry_转成_ProfileError(self, tmp_path: Path, monkeypatch):
        """registry 读文件时也要用严格加载器，否则门禁只在测试里生效。"""
        from app.services.graph.ontology import registry as reg_mod

        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "profile_id: bad\nversion: 1\ndomain: x\n"
            "entity_types:\n  - name: A\n    uid_prefix: aa\n    uid_prefix: bb\n",
            encoding="utf-8",
        )
        with pytest.raises(ProfileError) as exc:
            reg_mod.load_profile(bad)
        assert "重复键" in str(exc.value)
