"""MinerU 扫描 PDF 解析引擎封装（P1-2）。

MinerU（opendatalab）是复杂 PDF/扫描件的高质量解析器：版面分析 + OCR + 表格识别 +
阅读顺序恢复，输出结构化 JSON/markdown。本项目用它作为 RapidOCR 的高质量替代
（纯 CPU、离线、慢但准）。

本模块封装 CLI 调用（MinerU 以子进程方式运行，避免把 2.6GB 模型/推理塞进服务进程）：
- `run_mineru(path, out_dir)`：调用 `mineru` CLI 解析一个 PDF/PNG，输出到 out_dir
- `output_paths(out_dir)`：定位产物 _content_list.json / _middle.json
- 输出目录幂等：已有产物则跳过重跑（bake-off 可续跑）

P1-2 单元C 将在此模块加 `adapt_mineru_output()`（产物 → DocumentElement IR）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

MINERU_CMD = "mineru"
DEFAULT_ARGS = [
    "--backend", "pipeline",
    "--method", "ocr",
    "--lang", "ch",
    "--formula", "false",
    "--table", "true",
]

# MinerU 工具的全局配置 JSON（models-dir 指向本项目模型快照）
_TOOLS_CONFIG_NAME = "mineru-tools.json"


def _mineru_cli_path() -> Path | None:
    """定位 mineru CLI 可执行文件。

    优先当前 Python 环境的 Scripts 目录（venv 未激活时 PATH 可能不含它），
    其次全局 PATH。
    """
    import sys

    # 当前 venv 的 Scripts（Windows）或 bin（Unix）
    scripts_dir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    exe = scripts_dir / (MINERU_CMD + (".exe" if os.name == "nt" else ""))
    if exe.exists():
        return exe
    found = shutil.which(MINERU_CMD)
    if found:
        return Path(found)
    return None


def mineru_available() -> bool:
    """MinerU 是否已安装（CLI 可用）。"""
    if _mineru_cli_path() is None:
        return False
    try:
        import mineru  # noqa: F401
        return True
    except Exception:
        return False


def _models_snapshot_dir() -> Path | None:
    """定位模型快照目录（snapshots/master）。优先配置项，其次 data/mineru_models 约定路径。"""
    if settings.mineru_model_dir:
        p = Path(settings.mineru_model_dir)
        # 若直接指向快照目录（含 models/ 子结构）则用之；否则尝试拼接
        if (p / "models").exists():
            p = p / "models"
        return p
    # 默认：backend/data/mineru_models/modelscope/models/OpenDataLab--PDF-Extract-Kit-1.0/snapshots/master
    base = settings.data_dir / "mineru_models"
    candidates = [
        base / "modelscope" / "models" / "OpenDataLab--PDF-Extract-Kit-1.0" / "snapshots" / "master",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _ensure_tools_config() -> Path:
    """生成 MinerU 工具配置 JSON（models-dir 指向本项目模型快照），返回路径。"""
    import json

    snapshot = _models_snapshot_dir()
    if snapshot is None:
        raise RuntimeError(
            "未找到 MinerU 模型快照目录。请复制 Learn AI Agent/models/mineru 到 "
            "backend/data/mineru_models/，或设置 mineru_model_dir。"
        )
    tools_dir = settings.data_dir / "mineru_tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tools_dir / _TOOLS_CONFIG_NAME
    cfg = {
        "bucket_info": {},
        "llm-aided-config": {
            "title_aided": {"enable": False, "api_key": "", "base_url": "", "model": ""},
        },
        "models-dir": {
            "pipeline": str(snapshot),
            "vlm": "",
        },
        "model-source": "local",
        "config_version": "1.3.2",
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def _get_mineru_env() -> dict[str, str]:
    """构造 MinerU 子进程环境：本地模型 + CPU + 资源限制。"""
    env = os.environ.copy()
    env.update({
        "CUDA_VISIBLE_DEVICES": "",
        "MINERU_MODEL_SOURCE": settings.mineru_model_source or "local",
        "MINERU_TOOLS_CONFIG_JSON": str(_ensure_tools_config()),
        "MINERU_PDF_RENDER_THREADS": "1",
        "MINERU_PROCESSING_WINDOW_SIZE": "1",
        "MINERU_API_MAX_CONCURRENT_REQUESTS": "1",
        "MINERU_INTRA_OP_NUM_THREADS": "2",
        "MINERU_INTER_OP_NUM_THREADS": "1",
        "MINERU_TASK_RESULT_TIMEOUT_SECONDS": str(settings.mineru_timeout_sec),
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
    })
    return env


def output_paths(out_dir: Path) -> dict[str, Path | None]:
    """定位 MinerU 产物（幂等检查用）。返回 {content_list, middle, markdown}。

    MinerU 产物命名：`{stem}_content_list.json` / `{stem}_middle.json` / `{stem}.md`。
    """
    if not out_dir.exists():
        return {"content_list": None, "middle": None, "markdown": None}
    content = next(out_dir.rglob("*_content_list.json"), None)
    middle = next(out_dir.rglob("*_middle.json"), None)
    md = next(out_dir.glob("*.md"), None)
    return {
        "content_list": content,
        "middle": middle,
        "markdown": md,
    }


def run_mineru(path: Path, out_dir: Path, *, timeout_sec: int | None = None) -> dict:
    """调用 mineru CLI 解析一个 PDF/PNG，输出到 out_dir。返回产物路径字典。

    幂等：out_dir 下已有 _content_list.json 则跳过重跑（bake-off 可续跑）。
    """
    timeout = timeout_sec or settings.mineru_timeout_sec
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 幂等检查
    existing = output_paths(out_dir)
    if existing["content_list"] is not None:
        logger.info("MinerU 产物已存在，跳过: %s", out_dir)
        return existing

    cli = _mineru_cli_path()
    if cli is None:
        raise RuntimeError(
            "MinerU 未安装。请先 `pip install -r requirements-mineru.txt`（见 docs）。"
        )

    cmd = [str(cli), "--path", str(path), "--output", str(out_dir), *DEFAULT_ARGS]
    logger.info("运行 MinerU: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            env=_get_mineru_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("MinerU 超时（%ss）: %s", timeout, path)
        raise RuntimeError(f"MinerU 解析超时（{timeout}s）") from None

    if proc.returncode != 0:
        logger.error("MinerU 失败 rc=%s: %s", proc.returncode, (proc.stderr or "")[-2000:])
        raise RuntimeError(f"MinerU 解析失败: {(proc.stderr or '')[-500:]}")

    result = output_paths(out_dir)
    if result["content_list"] is None:
        logger.error("MinerU 完成但无 _content_list.json: %s", out_dir)
        raise RuntimeError("MinerU 完成但未产出 content_list.json")
    logger.info("MinerU 完成: %s", path)
    return result
