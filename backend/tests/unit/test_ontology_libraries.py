"""图谱库（多项目）：一个库 = 一份 TBox YAML。

覆盖：
- 列库：中文库名来自 YAML `display_name`，坏 YAML 也要列出来并带 error
- 建库：从模板复制时**保留注释**，只改 profile_id/display_name/description
- 建库：空白库只有最基本类型
- 删库：默认库、被系统配置引用的库不能删；不存在的库报可读错误
- 标识校验：只允许小写字母开头 + a-z0-9_
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import libraries as lib
from app.services.graph.ontology import registry as reg_mod
from app.services.graph.ontology.registry import ProfileError


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """把 profiles 目录换成临时目录，避免测试往真实目录里写库。"""
    real = reg_mod.PROFILES_DIR / "tuning.yaml"
    (tmp_path / "tuning.yaml").write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lib, "PROFILES_DIR", tmp_path)
    return tmp_path


class TestListLibraries:
    def test_display_name_comes_from_yaml(self, profiles):
        items = {i.profile_id: i for i in lib.list_libraries()}
        assert set(items) == {"tuning"}
        assert items["tuning"].display_name == "调参本体"
        assert items["tuning"].entity_type_count > 0

    def test_broken_yaml_is_listed_with_error(self, profiles):
        (profiles / "broken.yaml").write_text("version: 1\nprofile_id: broken\n", encoding="utf-8")
        items = {i.profile_id: i for i in lib.list_libraries()}
        assert "broken" in items, "坏库也要列出来，否则用户根本不知道它坏了"
        assert items["broken"].error


class TestCreateLibrary:
    def test_copy_template_keeps_comments_and_replaces_head(self, profiles):
        info = lib.create_library("flood_ctrl", "防洪调度本体", "防汛预案用")
        text = (profiles / "flood_ctrl.yaml").read_text(encoding="utf-8")

        assert info.profile_id == "flood_ctrl"
        assert info.display_name == "防洪调度本体"
        assert info.entity_type_count > 0, "复制模板应带上模板的类型"
        # 头部替换
        assert "profile_id: flood_ctrl" in text
        assert "display_name: 防洪调度本体" in text
        assert "profile_id: tuning" not in text
        # 注释是给维护者看的，不能因为复制就丢
        assert text.lstrip().startswith("#")

    def test_duplicate_is_rejected(self, profiles):
        lib.create_library("dup_lib", "重复库")
        with pytest.raises(ProfileError, match="已存在"):
            lib.create_library("dup_lib", "重复库")

    def test_blank_template_has_minimal_types(self, profiles):
        info = lib.create_library("blank_lib", "空白库", template=lib.TEMPLATE_BLANK)
        assert info.entity_type_count == 1
        assert info.relation_type_count == 0

    def test_invalid_profile_id(self, profiles):
        with pytest.raises(ProfileError, match="不合法"):
            lib.create_library("Bad-Name", "大写不行")

    def test_missing_template(self, profiles):
        with pytest.raises(ProfileError, match="模板"):
            lib.create_library("x_lib", "X", template="no_such_template")


class TestDeleteLibrary:
    def test_delete_removes_file(self, profiles):
        lib.create_library("tmp_lib", "临时库")
        lib.delete_library("tmp_lib")
        assert not (profiles / "tmp_lib.yaml").exists()

    def test_default_library_is_protected(self, profiles):
        with pytest.raises(ProfileError, match="默认图谱库"):
            lib.delete_library("tuning")

    def test_missing_library(self, profiles):
        with pytest.raises(ProfileError, match="不存在"):
            lib.delete_library("nope_lib")


class TestImportExport:
    def test_export_returns_text(self, profiles):
        filename, text = lib.read_library_text("tuning")
        assert filename == "tuning.yaml"
        assert "profile_id: tuning" in text

    def test_import_creates_new_library(self, profiles):
        text = (
            "version: 1\nprofile_id: imported\ndisplay_name: 导入的库\n"
            "entity_types:\n  - name: Thing\n    display_name: 东西\n"
            "    uid_prefix: tg\n    scope: local\nrelation_types: []\n"
        )
        info = lib.import_library(text)
        assert info.profile_id == "imported"
        assert info.display_name == "导入的库"
        assert (profiles / "imported.yaml").exists()

    def test_import_refuses_existing_library(self, profiles):
        text = "profile_id: tuning\ndisplay_name: x\nentity_types:\n  - name: A\n    uid_prefix: aa\n"
        with pytest.raises(ProfileError, match="已存在"):
            lib.import_library(text)

    def test_import_invalid_rolls_back(self, profiles):
        text = "profile_id: bad\ndisplay_name: x\nentity_types: []\n"
        with pytest.raises(Exception):
            lib.import_library(text)
        assert not (profiles / "bad.yaml").exists(), "导入失败不能留下半成品文件"

    def test_import_missing_library(self, profiles):
        with pytest.raises(ProfileError, match="不存在"):
            lib.read_library_text("nope")
