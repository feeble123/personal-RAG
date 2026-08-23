"""P1-2 单元A：MinerU 环境就绪冒烟测试。

- CLI 可用性（未装则 skip）
- 模型快照目录定位
- 工具配置 JSON 生成
- 输出产物路径定位（幂等检查）

核心逻辑是纯函数/可 mock，不依赖真 MinerU 运行。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.services.parser import mineru


class TestOutputPaths:
    def test_output_paths_locate_json(self, tmp_path):
        """人造 MinerU 产物 → 正确定位 content_list/middle/md。"""
        out = tmp_path / "out"
        out.mkdir(parents=True)
        (out / "x_content_list.json").write_text("[]", encoding="utf-8")
        (out / "x_middle.json").write_text("{}", encoding="utf-8")
        (out / "x.md").write_text("# t", encoding="utf-8")
        paths = mineru.output_paths(out)
        assert paths["content_list"] is not None
        assert paths["middle"] is not None
        assert paths["markdown"] is not None

    def test_output_paths_empty_dir(self, tmp_path):
        """空目录 → 全 None（幂等判断用）。"""
        out = tmp_path / "out"
        out.mkdir(parents=True)
        paths = mineru.output_paths(out)
        assert paths["content_list"] is None
        assert paths["middle"] is None


class TestModelsDir:
    def test_models_snapshot_dir_found(self, monkeypatch):
        """数据目录下模型快照存在 → 返回正确路径。"""
        # 用 settings.mineru_model_dir 指向真实存在的 data/mineru_models
        snapshot = settings.data_dir / "mineru_models" / "modelscope" / "models" / \
            "OpenDataLab--PDF-Extract-Kit-1.0" / "snapshots" / "master"
        if not snapshot.exists():
            pytest.skip("模型快照未就绪（单元A 复制模型后才有）")
        monkeypatch.setattr(settings, "mineru_model_dir", str(settings.data_dir / "mineru_models"))
        got = mineru._models_snapshot_dir()
        assert got is not None and got.exists()

    def test_tools_config_generated(self, monkeypatch, tmp_path):
        """生成的 tools config 指向模型快照 + model-source=local。"""
        # monkeypatch _models_snapshot_dir 返回人造快照目录（避免碰真实数据/pydantic property）
        fake_snapshot = tmp_path / "models" / "OpenDataLab--PDF-Extract-Kit-1.0" / "snapshots" / "master"
        fake_snapshot.mkdir(parents=True)
        monkeypatch.setattr(mineru, "_models_snapshot_dir", lambda: fake_snapshot)
        # 也 monkeypatch data_dir 生成位置（用 monkeypatch 设置 mineru_model_dir 间接避免 pydantic property）
        monkeypatch.setattr(settings, "mineru_model_dir", str(tmp_path))

        # _ensure_tools_config 用 settings.data_dir 定位 tools 目录；直接 monkeypatch 该 property
        import types
        monkeypatch.setattr(type(settings), "data_dir", property(lambda self: tmp_path))

        cfg_path = mineru._ensure_tools_config()
        assert cfg_path.exists()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert cfg["model-source"] == "local"
        assert str(fake_snapshot) in cfg["models-dir"]["pipeline"]


class TestRunMineru:
    def test_run_mineru_idempotent_skips(self, tmp_path):
        """产物已存在 → 跳过重跑（不调 subprocess）。"""
        out = tmp_path / "out"
        out.mkdir(parents=True)
        (out / "x_content_list.json").write_text("[]", encoding="utf-8")
        result = mineru.run_mineru(tmp_path / "a.pdf", out)
        assert result["content_list"] is not None

    def test_run_mineru_unavailable_raises(self, tmp_path, monkeypatch):
        """MinerU 未安装 → 报错（不真跑）。"""
        monkeypatch.setattr(mineru, "_mineru_cli_path", lambda: None)
        out = tmp_path / "out"
        with pytest.raises(RuntimeError, match="未安装"):
            mineru.run_mineru(tmp_path / "a.pdf", out)

    def test_mineru_cli_available(self):
        """CLI 可用性冒烟（未装则 skip）。"""
        if not mineru.mineru_available():
            pytest.skip("MinerU 未安装，跳过 CLI 冒烟")
        assert mineru.mineru_available() is True
