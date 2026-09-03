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
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

MINERU_CMD = "mineru"
# 后端无关的公共参数（pipeline 与 hybrid-engine 都用）
_COMMON_ARGS = [
    "--lang", "ch",
    "--formula", "true",
    "--table", "true",
]


def _build_mineru_args(backend: str | None = None) -> list[str]:
    """按 settings 构建 MinerU CLI 参数。

    - pipeline 后端：老一代版面+OCR，用 --method ocr（扫描件）/ txt（文字层）
    - hybrid-engine：新一代 pipeline+VLM 组合，用 --effort medium/high

    Args:
        backend: 可选后端覆盖（单元 S：文档级 parse_mode 覆盖全局默认）。
                 省略时读 settings.mineru_backend。
    """
    backend = backend or settings.mineru_backend
    args: list[str] = []
    if backend == "hybrid-engine":
        args += ["--backend", "hybrid-engine", "--effort", settings.mineru_effort]
    else:
        # 默认 pipeline：扫描件用 ocr；method 由扫描占比决定（这里保守走 ocr）
        args += ["--backend", "pipeline", "--method", "ocr"]
    return args + _COMMON_ARGS

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


def _vlm_snapshot_dir() -> Path | None:
    """定位 VLM 模型快照目录（hybrid-engine 所需，MinerU2.5-Pro-2605-1.2B）。

    模型经 modelscope 下载后落在 data/mineru_models/modelscope/models/OpenDataLab--MinerU2.5-Pro-2605-1.2B/snapshots/<hash>。
    返回该目录（存在才返回，未下载则 None）。
    """
    base = settings.data_dir / "mineru_models" / "modelscope" / "models"
    candidates = [
        base / "OpenDataLab--MinerU2.5-Pro-2605-1.2B",
        base / "OpenDataLab/MinerU2.5-Pro-2605-1.2B",
    ]
    for c in candidates:
        if not c.exists():
            continue
        snapshots = c / "snapshots"
        if snapshots.exists():
            for snap in sorted(snapshots.iterdir(), reverse=True):
                if snap.is_dir() and any(snap.glob("*.safetensors")) or any(snap.glob("*.json")):
                    return snap
        # 直接指向 model.safetensors 所在目录（modelscope 可能不带 snapshots 层）
        if any(c.glob("*.safetensors")):
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
    vlm_snapshot = _vlm_snapshot_dir()
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
            "vlm": str(vlm_snapshot) if vlm_snapshot else "",
        },
        "model-source": "local",
        "config_version": "1.3.2",
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def _get_mineru_env() -> dict[str, str]:
    """构造 MinerU 子进程环境：本地模型 + CPU/GPU + 资源限制。"""
    env = os.environ.copy()
    # GPU 策略（P1-2）：mineru_device=gpu 时启用 CUDA，否则禁用（保持 CPU 模式）
    use_gpu = settings.mineru_device == "gpu"
    env.update({
        "CUDA_VISIBLE_DEVICES": "0" if use_gpu else "",
        "MINERU_MODEL_SOURCE": settings.mineru_model_source or "local",
        "MINERU_TOOLS_CONFIG_JSON": str(_ensure_tools_config()),
        "MINERU_PDF_RENDER_THREADS": "1",
        # 关键修复：window_size 从 1 改为 64（MinerU 默认值）——
        # 1 会导致 547 页拆成 547 批，每批重复初始化公式模型，内存尖峰 OOM；
        # 64 页一批则 547 页仅 ~9 批，批间释放内存，不会 OOM。
        "MINERU_PROCESSING_WINDOW_SIZE": "64",
        "MINERU_API_MAX_CONCURRENT_REQUESTS": "1",
        "MINERU_INTRA_OP_NUM_THREADS": "2",
        "MINERU_INTER_OP_NUM_THREADS": "1",
        "MINERU_TASK_RESULT_TIMEOUT_SECONDS": str(settings.mineru_timeout_sec),
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        # 单元 E 修复：Windows 下 MinerU 子进程输出 UTF-8 中文，
        # 父进程若按 locale（GBK）解码会抛 UnicodeDecodeError（线程内报错，干扰日志）。
        # 这里让子进程强制 UTF-8 输出，配合 run_mineru 里 encoding="utf-8" 双保险。
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    return env


def output_paths(out_dir: Path) -> dict[str, Path | None]:
    """定位 MinerU 产物（幂等检查用）。返回 {content_list, middle, markdown}。

    MinerU 产物命名：`{stem}_content_list.json` / `{stem}_middle.json` / `{stem}.md`。
    """
    if not out_dir.exists():
        return {"content_list": None, "middle": None, "markdown": None}
    # 注意：hybrid 后端会同时产出 _content_list.json（扁平块列表）与
    # _content_list_v2.json（按页嵌套）。adapter 消费扁平版，故排除 _v2 后缀。
    content = next(
        (p for p in out_dir.rglob("*_content_list.json") if "_v2" not in p.name),
        None,
    )
    middle = next(out_dir.rglob("*_middle.json"), None)
    md = next(out_dir.glob("*.md"), None)
    return {
        "content_list": content,
        "middle": middle,
        "markdown": md,
    }


def run_mineru(path: Path, out_dir: Path, *, timeout_sec: int | None = None, force: bool = False, backend: str | None = None) -> dict:
    """调用 mineru CLI 解析一个 PDF/PNG，输出到 out_dir。返回产物路径字典。

    force=False（默认）：out_dir 已有 _content_list.json 则跳过重跑（bake-off 续跑用）。
    force=True：删除旧产物，强制 MinerU 重新解析（入库重灌必须用，保证真正重跑）。
    backend：可选后端覆盖（单元 S：文档级 parse_mode 覆盖全局默认）。
    """
    timeout = timeout_sec or settings.mineru_timeout_sec
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # force=True 时删除旧产物，强制重新解析
    if force and out_dir.exists():
        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
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

    cmd = [str(cli), "--path", str(path), "--output", str(out_dir), *_build_mineru_args(backend)]
    logger.info("运行 MinerU: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            env=_get_mineru_env(),
            capture_output=True,
            text=True,
            # 单元 E 修复：显式 UTF-8 解码 + errors="replace" 容错。
            # Windows 默认按 locale（GBK）解码子进程 stdout/stderr，MinerU 输出的
            # UTF-8 中文会触发 UnicodeDecodeError（_readerthread 线程内报错）。
            encoding="utf-8",
            errors="replace",
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


# =====================================================================
# P1-2 单元C：MinerU 产物 → 本项目 DocumentElement IR（adapter）
# =====================================================================

# MinerU 元素类型 → 本项目 ElementType 映射（排除项过滤）
_EXCLUDED_MINERU_TYPES = frozenset({"footer", "page_number"})
# 正则：检测编号标题（MinerU 漏标 text_level 时的回退）
# 「6 避洪转移分析」→ level 1；「4.4.2 基础资料」→ level 3；「附录A」→ level 1
_HEADING_GUESS = re.compile(r"^(?:(?:附录)?[A-Z])|(?:(\d+(?:\.\d+)*)\s+\S)")


# ============ 大纲编号通用解析（单元 A） ============
# 统一识别多种大纲编号体系，输出规范阿拉伯数字点分编号。新老 MinerU
# （pipeline / hybrid-engine）共用此解析，不再拿「水力学」的阿拉伯点分当唯一标准。
# 支持体系（用户列出的常见形态，可扩展）：
#   一级：`1` / `一` / `第1章` / `第一章`
#   二级：`1.1` / `（一）` / `第1节` / `第一节`
#   三级：`1.1.1`（阿拉伯点分；`1）` 属列表项，不认作大纲）
# 相对节号（`第N节`/`第一节`/`（一）`）的章号用占位符 `C` 表示，由调用方
# 结合当前章上下文替换为真实章号。

_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(s: str) -> int | None:
    """中文数字 → 阿拉伯（支持 一~九十九）。非中文数字返回 None。"""
    s = s.strip()
    if not s:
        return None
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if "十" not in s:
        return None
    parts = s.split("十")
    if len(parts) > 2:
        return None  # 百位及以上不支持（水利文档章节号不会到百）
    tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1  # 「十」无前缀=10
    ones = _CN_DIGITS.get(parts[1], 0) if parts[1] else 0
    return tens * 10 + ones


def _parse_outline_number(text: str) -> tuple[str, int, str] | None:
    """通用大纲编号解析：标题行 → (规范阿拉伯编号, 层级, 标题) 或 None。

    通用化（单元 A）：支持多种大纲编号体系，统一转成阿拉伯数字点分编号。
    相对节号（`第N节`/`第一节`/`（一）`）的章号用占位符 ``C`` 表示，由调用方
    结合当前章上下文替换为真实章号。标题为编号之后到行尾的原始剩余，
    目录场景由 _extract_toc_map 再剥页码，正文场景由 _normalize_section_title 清洗。
    """
    import re

    t = text.strip()
    if not t or len(t) > 40:
        return None
    # 公式/日期守卫：含数学符号或「NNNN年」不是标题
    if any(c in t for c in "φ√αβγμρΣ∫→=+∂∇∮"):
        return None
    if re.match(r"^\d{3,4}年", t):
        return None
    # 清洗点号后空格：`1. 4` → `1.4`（编号粘连）
    t = re.sub(r"(\d)\s*\.\s*(?=\d)", r"\1.", t)

    # 1) 章节式：第N章 / 第一章 → 一级
    m = re.match(r"^第\s*(\d{1,2})\s*章\s*([一-鿿A-Za-z].*)$", t)
    if m:
        return m.group(1), 1, m.group(2).strip()
    m = re.match(r"^第\s*([零一二两三四五六七八九十]+)\s*章\s*([一-鿿A-Za-z].*)$", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            return str(n), 1, m.group(2).strip()
    # 2) 章节式：第N节 / 第一节 → 二级（章号占位 C）
    m = re.match(r"^第\s*(\d{1,2})\s*节\s*([一-鿿A-Za-z].*)$", t)
    if m:
        return f"C.{m.group(1)}", 2, m.group(2).strip()
    m = re.match(r"^第\s*([零一二两三四五六七八九十]+)\s*节\s*([一-鿿A-Za-z].*)$", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            return f"C.{n}", 2, m.group(2).strip()
    # 3) 中文括号：（）/（一） → 二级（章号占位 C）
    m = re.match(r"^[（(]\s*([零一二两三四五六七八九十]+)\s*[)）]\s*([一-鿿A-Za-z].*)$", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            return f"C.{n}", 2, m.group(2).strip()
    # 4) 阿拉伯点分：1.1 / 1.1.1 → 层级 = 点数 + 1
    m = re.match(r"^(\d{1,2}(?:\.\d+)+)\s*([一-鿿].*)$", t)
    if m:
        num = m.group(1)
        return num, num.count(".") + 1, m.group(2).strip()
    # 5) 阿拉伯单数字：1 + 中文 → 一级（排除「1. 列表项」）
    m = re.match(r"^(\d{1,2})\s*([一-鿿].*)$", t)
    if m:
        after_num = t[m.end(1):]
        if re.match(r"^\s*\.", after_num):
            return None  # 「1. 总压力」是列表项，非章标题
        return m.group(1), 1, m.group(2).strip()
    # 6a) 中文数字 + 顿号/点：一、xxx / 一.xxx → 一级（最可靠）
    m = re.match(r"^([零一二两三四五六七八九十]+)\s*[、.．]\s*([一-鿿].*)$", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            return str(n), 1, m.group(2).strip()
    # 6b) 中文数字 + 空格 + 短标题：一 xxx（标题 ≤ 12 字，防「一般来说」误判）
    m = re.match(r"^([零一二两三四五六七八九十]{1,2})\s+([一-鿿][^。！？；\n]{0,11})$", t)
    if m:
        n = _cn_to_int(m.group(1))
        if n:
            return str(n), 1, m.group(2).strip()
    return None


def _strip_toc_page_number(line: str) -> str:
    """剥离目录行末尾的省略号+页码 / 粘连页码，返回「编号+标题」部分。

    形态：`1.1 水力学的任务…………………1` / `2.5 水资源综合评价62` / `第2章水资源评价……12`。
    """
    import re

    line = line.strip()
    # 带省略号：…页码
    m = re.match(r"^(.*?)\s*[.·…]{1,}\s*\d{1,4}\s*$", line)
    if m:
        return m.group(1).strip()
    # 无省略号：末尾「中文/括号 + 数字页码」粘连
    m = re.match(r"^(.*[一-鿿）)])\s*\d{1,4}\s*$", line)
    if m:
        return m.group(1).strip()
    return line


def _extract_toc_map(content_list: list[dict]) -> dict[str, tuple[str, int]]:
    """从 MinerU 的 content_list 提取目录权威骨架：{编号 → (标题, level)}。

    通用化（单元 A）：用 _parse_outline_number 识别多种大纲编号体系
    （`1`/`一`/`第1章`/`第一章` 一级；`1.1`/`（一）`/`第N节`/`第一节` 二级），
    不再只认阿拉伯点分。相对节号（`第N节`/`（一）`）的章号由当前章上下文补全。

    只收一二级编号（level ≤ 2），构建 {编号 → (标题, level)} 权威映射。
    """
    import re

    # 定位目录文本块：含多个「可剥页码 + 可解析大纲编号」行的多行文本。
    # 单元 B 修复：目录常跨多页、被 MinerU 拆成多个文本块（如 p10 第1~4章、
    # p11 4.5~第9章），此前用 max() 只取数字最多的一块 → 前面的章整段丢失。
    # 改为：按出现顺序合并所有目录块，完整覆盖全书大纲。
    # 判定收紧为「剥页码后有变化 且 能解析大纲」——正文列表页（无页码）不会误入。
    toc_blocks: list[str] = []
    for item in content_list:
        txt = (item.get("text") or "")
        if not isinstance(txt, str) or not txt.strip():
            continue
        numbered = 0
        for line in txt.split("\n"):
            body = _strip_toc_page_number(line)
            if body != line.strip() and _parse_outline_number(body) is not None:
                numbered += 1
        if numbered >= 5:  # 目录块：至少 5 个「带页码的目录行」
            toc_blocks.append(txt)

    if not toc_blocks:
        return {}

    toc_text = "\n".join(toc_blocks)

    toc_map: dict[str, tuple[str, int]] = {}
    current_chapter: str | None = None  # 当前章号（阿拉伯），用于补全相对节号 C.N
    for line in toc_text.split("\n"):
        body = _strip_toc_page_number(line)
        parsed = _parse_outline_number(body)
        if parsed is None:
            continue
        num, level, title = parsed
        if level > 2:
            continue  # 只收一二级
        title = re.sub(r"\s+", "", title).strip()
        if not title:
            continue
        # 相对节号（第N节/（一））→ 用当前章补全
        if num.startswith("C."):
            if current_chapter is None:
                continue
            num = f"{current_chapter}.{num[2:]}"
        elif level == 1:
            current_chapter = num  # 更新当前章号
        if not num or not title:
            continue
        toc_map[num] = (title, level)
    return toc_map


def _guess_heading_level(text: str) -> int | None:
    """MinerU 未标 text_level 时，用通用解析器推断标题层级。

    只推断 1/2 级：「6 避洪转移分析」→ 1；「7.4 地图版面布局」→ 2。
    三级及以上（「4.6.16」）不推断——保留为正文，放在切片 body 里。
    单元 A：改用 _parse_outline_number，同时支持中文数字/章节式编号体系。
    """
    t = text.strip()
    if len(t) <= 2:
        return None
    parsed = _parse_outline_number(t)
    if parsed is None:
        return None
    _num, level, _title = parsed
    return level if level in (1, 2) else None


def _normalize_section_title(text: str) -> str:
    """规范化 section 标题：编号与标题之间统一为「编号 + 空格 + 标题」。

    「1绪论」→「1 绪论」；「6明渠流动」→「6 明渠流动」；「2.5作用」→「2.5 作用」。
    消除 MinerU OCR 的空格不一致，让同一章节的 section 文本统一。
    单元 A：中文数字编号（一、/（一））也转阿拉伯点分，保证 section 跨体系统一。
    """
    import re

    t = text.strip()
    parsed = _parse_outline_number(t)
    if parsed is not None:
        num, _level, title = parsed
        # 相对节号（第N节/（一））保留占位形式，由调用方上下文补全
        clean = re.sub(r"\s+", "", title)
        if num.startswith("C."):
            return f"{num[2:]} {clean}".rstrip() if clean else t
        return f"{num} {clean}".rstrip() if clean else t
    # 兜底：编号 + 标题粘连（无空格）→ 加空格
    m = re.match(r"^(\d+(?:\.\d+)*)([一-鿿])", t)
    if m:
        return f"{m.group(1)} {m.group(2)}{t[m.end():]}".rstrip()
    return t


def _parse_chapter_heading(text: str) -> tuple[str, str] | None:
    """识别「第N章 标题」格式的章标题，返回 (章号, 标题) 或 None。

    单元 A：章号支持阿拉伯（第1章）与中文数字（第一章）；标题内部空格压缩。
    """
    parsed = _parse_outline_number(text)
    if parsed is None:
        return None
    num, level, title = parsed
    if level != 1:
        return None
    if "章" not in text:
        return None  # 仅章节式（第N章/第一章）算章标题，阿拉伯「1 xx」由调用方处理
    return num, title


def _parse_numbered_heading(text: str) -> tuple[str, int] | None:
    """统一解析编号标题，返回 (编号, 层级) 或 None。

    单元 A：改用通用 _parse_outline_number，支持阿拉伯点分 + 中文数字 + 章节式。
    相对节号（第N节/（一））返回占位章号（C.N），由调用方按当前章上下文补全。
    """
    parsed = _parse_outline_number(text)
    if parsed is None:
        return None
    num, level, _title = parsed
    return num, level


def _clean_latex(text: str) -> str:
    """清理 LaTeX 公式标记，转为可检索文字。

    $$\rho = \frac{m}{V}\tag{1.3}$$ → ρ = m/V (1.3)
    保留希腊字母、运算符和编号，去掉 LaTeX 命令与定界符。
    保留下标 _ 和上标 ^（可检索可见）；\tag{...} → (...)。
    兜底清洗 LaTeX 转义标点（\| \~ \% \_ \^ \* \\空格）——这些「反斜杠+非字母」
    不是命令，旧正则捕不到，会残留成「\| \|_\|_」乱码。
    """
    import re

    if not text:
        return ""
    t = text.strip()
    # 0) 移除 \begin{...} 和 \end{...} 环境（含参数，如 \begin{array}{ll}）
    t = re.sub(r"\\begin\{[^}]*\}", " ", t)
    t = re.sub(r"\\end\{[^}]*\}", " ", t)
    # 1) \tag{...} → ( ... ) 先处理（避免花括号被后续步骤剥离后丢失括号）
    #    前面加空格：`v_2\tag{3.15}` → `v_2 (3.15)`，编号不与公式粘连
    t = re.sub(r"\\tag\s*\{([^}]*)\}", r" (\1)", t)
    # 2) 替换 LaTeX 命令为对应符号（\rho→ρ、\frac→/、\mathrm→空 等）
    t = re.sub(r"\\[a-zA-Z]+", lambda m: _LATEX_GREEK.get(m.group(0), " "), t)
    # 2.5) 转义标点兜底：反斜杠 + 非字母（\| \~ \% \_ \^ \* \; \: \空格）。
    #       第 2 步只捕「反斜杠+字母」命令，这些特殊字符的 LaTeX 转义捕不到，
    #       原样残留成「\| \|_\|_」乱码。有检索含义的还原成本字符（% ~ _ ^），
    #       纯噪声/空格命令（\| \* \; \:）替换成空格。
    _latex_esc = {"%": "%", "~": "~", "_": "_", "^": "^"}
    t = re.sub(r"\\([%~_^|*;:])", lambda m: _latex_esc.get(m.group(1), " "), t)
    t = re.sub(r"\\ ", " ", t)  # 反斜杠+空格（强制空格）
    # 3) 去掉花括号（{0} → 0），但保留内容
    t = t.replace("{", "").replace("}", "")
    # 3.5) 紧凑化下标/上标：`A _ { 1 }` → `A_1`、`L ^ { 2 }` → `L^2`（去 _/^ 前后空格）
    t = re.sub(r"\s*([_^])\s*", r"\1", t)
    # 4) 去掉行内/行间定界符 $
    t = t.replace("$", " ")
    # 5) 清理 LaTeX 表格对齐符 & 和换行符 \\（array 环境残留）
    t = t.replace("\\\\", " ").replace("&", " ")
    # 6) 压缩连续空格
    t = re.sub(r"\s+", " ", t).strip()
    return t


# LaTeX 命令 → 希腊字母/符号映射（公式可检索性）
_LATEX_GREEK = {
    "\\rho": "ρ", "\\mu": "μ", "\\sigma": "σ", "\\alpha": "α", "\\beta": "β",
    "\\gamma": "γ", "\\delta": "δ", "\\lambda": "λ", "\\theta": "θ", "\\omega": "ω",
    "\\pi": "π", "\\phi": "φ", "\\psi": "ψ", "\\eta": "η", "\\tau": "τ",
    "\\times": "×", "\\cdot": "·", "\\leq": "≤", "\\geq": "≥", "\\neq": "≠",
    "\\approx": "≈", "\\pm": "±", "\\infty": "∞", "\\sum": "∑", "\\int": "∫",
    "\\partial": "∂", "\\nabla": "∇", "\\sqrt": "√",
    # 结构/字体命令 → 空（不影响可检索性）
    "\\frac": "/", "\\mathrm": "", "\\textrm": "", "\\mathbf": "", "\\mathit": "",
    "\\left": "", "\\right": "", "\\overline": "", "\\underline": "",
    "\\begin": "", "\\end": "", "\\tag": " ", "\\text": "",
    "\\varOmega": "Ω", "\\varepsilon": "ε", "\\varphi": "φ", "\\prime": "′",
    "\\varrho": "ρ", "\\varpi": "ϖ", "\\varsigma": "ς",
    "\\qquad": " ", "\\quad": " ", "\\limits": "", "\\displaystyle": "",
}


def _norm_mineru_text(text: str) -> str:
    """文本清洗：去中文间多余空格 / 日期连字符 / 表题归一（参考参考项目实测清洗）。

    容错：MinerU 某些元素的 text/table_caption 可能是 list（多行文本），
    此时转换为空格拼接的字符串。
    """
    import re

    if isinstance(text, list):
        text = " ".join(str(t) for t in text if t)
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    t = text.strip()
    t = re.sub(r"(?<=[㐀-鿿])\s+(?=[㐀-鿿])", "", t)  # 中文间空格
    t = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", t)  # 日期连字符
    # 小数空格合并：MinerU 把公式里的小数拆散成「2 . 5」「0 . 9 5」。
    # 只合并「点号两侧都有空格」的小数点（说明是被拆散的），并继续合并小数点
    # 右侧被空格拆散的各位数字（0 . 9 5 → 0.95）。
    # 守卫：点号前必须是数字（lookbehind）；正常小数（3.4，点两侧无空格）不动；
    #       列表项「1. 2」/ 编号「9.4. 3」（点前无空格）不动。
    # 不碰「整数拆散」（13 → 1 3）：数字+空格+数字 的假阳性太多（页码+章节号、
    # 编号列表），无法安全区分，风险过高。
    t = re.sub(
        r"(?<=\d)\s+\.\s+\d(?:\s+\d)*",
        lambda m: re.sub(r"\s+", "", m.group(0)),
        t,
    )
    t = re.sub(r"^(表\s*\d+)", lambda m: m.group(1).replace(" ", ""), t)  # 表 1 → 表1
    return t


def _parse_table_html(html: str) -> dict | None:
    """解析 MinerU table_body HTML → {rows, header_path}（供 IR table 字段）。

    容错：解析失败降级为纯文本行（不抛异常）。
    """
    import re

    if not html:
        return None
    try:
        rows_raw = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
        rows: list[list[str]] = []
        for r in rows_raw:
            cells = [
                re.sub(r"<[^>]+>", "", c).strip()
                for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL | re.IGNORECASE)
            ]
            if cells and any(c for c in cells):
                rows.append(cells)
        if not rows:
            return None
        header_path = rows[0]
        return {"rows": rows, "header_path": header_path}
    except Exception:  # noqa: BLE001  容错降级
        logger.debug("MinerU 表格 HTML 解析失败，降级纯文本", exc_info=True)
        return None


def adapt_mineru_output(
    content_list: list[dict],
    middle: dict | None = None,
    *,
    doc_name: str = "document",
) -> list["DocumentElement"]:
    """把 MinerU 的 _content_list.json 映射为 DocumentElement 列表。

    - header/footer/page_number 排除
    - text 带 text_level → HEADING（MinerU 版面模型直接给层级，不标 inferred_heading）
    - table → TABLE（table_body HTML 解析成 table 字段）
    - image → FIGURE
    - 顺序 = content_list 顺序（MinerU 已按阅读顺序排布）→ reading_order
    - **section_path 按 heading 层级 + TOC 校准构建**：从目录页提取权威章节编号→标题映射，
      覆盖 MinerU 不准确的 heading 检测（尤其教科书公式被误判为标题的问题）
    """
    from app.services.parser.ir import DocumentElement, ElementType

    # P1-2 单元3：提取目录权威骨架 {编号 → (标题, level)}，用于校验标题
    toc_map = _extract_toc_map(content_list)
    if toc_map:
        logger.info("MinerU TOC 骨架提取: %d 条章节映射", len(toc_map))

    elements: list[DocumentElement] = []
    section_stack: list[str] = []  # 标题栈：按层级累积 section_path
    stack_touched = False  # 是否已遇到第一个数字编号标题（跳过封面书名）
    last_header_key: str | None = None  # header 连续去重：记录上一个页眉文本
    pending_bare_number: str | None = None  # 裸编号标题缓冲（如 "3.4" — 等下一个元素合并）
    order = 0  # 排除 header/footer 后重新编号（validator 要求 reading_order 连续 = 列表序）
    for idx, raw in enumerate(content_list):
        mtype = raw.get("type") or "text"
        if mtype in _EXCLUDED_MINERU_TYPES:
            continue
        text = _norm_mineru_text(raw.get("text") or "")

        # Merging bare-number heading: MinerU splits "3.4 专题洪水风险图" into
        # [lvl=2 "3.4"] + [lvl=None "专题洪水风险图"]. Merge the number into the next element.
        if pending_bare_number:
            if text:
                text = pending_bare_number + " " + text
            else:
                text = pending_bare_number
            pending_bare_number = None
            # also carry over the heading level from the bare number
            if raw.get("text_level") is None:
                raw = {**raw, "text_level": heading_level}
            heading_level = heading_level  # preserve from previous iteration

        if mtype == "table":
            etype = ElementType.TABLE
            table = _parse_table_html(raw.get("table_body") or "")
            heading_level = None
            # 表格文本：优先表题（table_caption），空则用解析出的行拼接（避免空文本）
            if not text:
                caption = _norm_mineru_text(raw.get("table_caption") or "")
                if caption:
                    text = caption
                elif table and table["rows"]:
                    text = " | ".join(" | ".join(row) for row in table["rows"][:5])
            # P1-2 单元5补充：表格单元格含行内公式时也清理 LaTeX（如表头 $h/m$）
            if text and ("$" in text or "\\" in text):
                text = _clean_latex(text)
        elif mtype == "image":
            etype = ElementType.FIGURE
            table = None
            heading_level = None
            # 图片 text 为空时用 caption（如 "图1 河道洪水计算范围示意图"）
            if not text:
                cap = raw.get("image_caption") or raw.get("table_caption") or ""
                if isinstance(cap, list):
                    cap = " ".join(str(c) for c in cap if c)
                cap = _norm_mineru_text(cap)
                if cap:
                    text = cap
            # 单元 A 补漏：caption 也为空时，在 IR 层生成可检索占位「图 pXX」。
            # 此前占位只在 blocks 兼容层（mineru_to_blocks）生成，但切片走 elements 层，
            # 空 figure 被 _atoms_from_elements 跳过 → 占位从未进检索，还触发完整性误报。
            if not text.strip():
                pidx = raw.get("page_idx")
                page_no = pidx + 1 if isinstance(pidx, int) else None
                text = f"图 p{page_no}" if page_no else "图"
            # P1-2 单元5补充：图注含行内公式时也清理 LaTeX（如「(a) $0<t<\frac{L}{a}$」）
            if text and ("$" in text or "\\" in text):
                text = _clean_latex(text)
        elif mtype == "equation":
            # 公式：MinerU 公式识别的结果是 LaTeX（如 $$\rho = \frac{m}{V}\tag{1.3}$$）
            # 清理 LaTeX 标记，保留可检索的公式文字（如 ρ = m/V (1.3)）
            etype = ElementType.PARAGRAPH
            table = None
            heading_level = None
            if not text:
                text = _norm_mineru_text(raw.get("text") or raw.get("latex") or "")
            text = _clean_latex(text)
        elif mtype in ("title", "text", "header"):
            # MinerU 把「1 绪论」标为 type=header, text_level=None
            # header 也走标题检测，避免章节标题被当成段落
            lvl = raw.get("text_level")
            table = None
            if lvl is not None and lvl >= 1:
                heading_level = int(lvl)
                # MinerU 把「3.4」和「专题洪水风险图」拆成两行：
                # 裸编号标题（如 "3.4" text≤5字）→ 缓冲，合并到下一个元素
                if heading_level >= 2 and re.match(r"^\d+(?:\.\d+)?$", text):
                    pending_bare_number = text
                    continue
                # P1-2 单元3：统一用「编号层级推导」——_parse_numbered_heading 返回 (编号, 层级)
                parsed = _parse_numbered_heading(text)
                if parsed:
                    num, num_level = parsed
                    # 三级及以上编号（如 1.3.4）→ 不进 section，但锚定前缀到二级
                    if num_level >= 3:
                        etype = ElementType.PARAGRAPH
                        heading_level = None
                        # 锚定前缀取前两段（1.3.4 → 1.3；1.3.4.5 → 1.3），
                        # 让三级/四级标题下跨页正文保持二级 section。
                        # 此前用 [:-1]（去最后一段）会把四级 1.3.4.5 锚到三级 1.3.4，
                        # 而 TOC 无三级条目 → 兜底把三级编号塞成二级 section（泄漏）。
                        anchor_prefix = ".".join(num.split(".")[:2])
                        if stack_touched:
                            if anchor_prefix in toc_map and toc_map[anchor_prefix][1] == 2:
                                section_stack = section_stack[:1]
                                section_stack.append(f"{anchor_prefix} {toc_map[anchor_prefix][0]}")
                            elif len(section_stack) >= 1:
                                section_stack = section_stack[:1]
                                section_stack.append(anchor_prefix)
                    # 一二级编号 → 进 section
                    elif num_level in (1, 2):
                        # 目录权威骨架校验：编号必须在 TOC，且用 TOC 权威标题覆盖
                        if toc_map and num in toc_map:
                            toc_title, toc_level = toc_map[num]
                            text = f"{num} {toc_title}"
                            heading_level = toc_level
                        elif toc_map and num not in toc_map:
                            # 编号不在 TOC（习题/页眉误判）→ 正文
                            etype = ElementType.PARAGRAPH
                            heading_level = None
                        else:
                            heading_level = num_level
                        if heading_level in (1, 2):
                            if heading_level == 1 and not stack_touched:
                                section_stack.clear()
                                stack_touched = True
                            etype = ElementType.HEADING
                            if heading_level <= len(section_stack):
                                section_stack = section_stack[: heading_level - 1]
                            # P1-2 单元4修复：跨章二级标题（如 3.1）自动将一级更新为对应章
                            # MinerU 把页眉放在正文之后——3.1 出现在 header「3 液体运动的流束理论」之前，
                            # 此时栈中一级仍是「2 水静力学」，形成混乱的「2 水静力学 / 3.1」。
                            # 修复：二级标题检测到章号不匹配时，从 TOC 查找并更新一级。
                            if heading_level == 2 and parsed and num_level == 2:
                                chapter_num = num.split(".")[0]
                                if toc_map and chapter_num in toc_map:
                                    chapter_title = f"{chapter_num} {toc_map[chapter_num][0]}"
                                    if not section_stack:
                                        section_stack = [chapter_title]
                                    elif section_stack[0].split()[0] != chapter_num:
                                        section_stack[0] = chapter_title
                            section_stack.append(_normalize_section_title(text.split("\n")[0].strip()))
                else:
                    # 非编号标题（如「思考题」「习题」「参考文献」「附录」）——
                    # 它们是章节附属标记，不是一二级大纲，**不进 section**（用户要求 section 只含一二级）。
                    # 保留为 HEADING（前端目录可显示、切片作边界），但 section_path 维持当前栈不变。
                    if len(text) <= 15 and heading_level in (1, 2) and re.match(r"^[一-鿿\s]+$", text):
                        etype = ElementType.HEADING
                        # 不更新 section_stack：习题/思考题/参考文献 不属于一二级大纲
                    else:
                        etype = ElementType.PARAGRAPH
                        heading_level = None
            else:
                # MinerU 未标 text_level（header 页眉 / title）时，用正则回退检测编号标题
                inferred_level = _guess_heading_level(text)
                # P1-2 单元3：目录权威骨架校验——编号必须匹配 TOC，
                # 且 MinerU 标题与 TOC 标题一致（不一致=习题/页眉误判，判为正文）
                if inferred_level and toc_map:
                    num_clean = re.match(r"^(\d+(?:\.\d+)?)", text)
                    if num_clean:
                        num_key = num_clean.group(1)
                        if num_key in toc_map:
                            toc_title, toc_level = toc_map[num_key]
                            # 提取 MinerU 给的标题（编号后）
                            mineru_title = re.sub(r"^\d+(?:\.\d+)?\s*", "", text).strip()
                            # 标题一致性：MinerU 标题是 TOC 标题的子串/前缀才认作真标题
                            if mineru_title and mineru_title[:2] != toc_title[:2]:
                                inferred_level = None
                            else:
                                # 真标题：用 TOC 权威标题 + level 覆盖
                                text = f"{num_key} {toc_title}"
                                inferred_level = toc_level
                        else:
                            inferred_level = None
                if inferred_level:
                    # 「第N章」header → 规范化为「N 标题」（与 toc_map 章条目格式一致），
                    # 使一级 section 显示「1 绪论」而非「第1章 绪 论」，且章号能对齐。
                    ch = _parse_chapter_heading(text)
                    if ch:
                        ch_num, ch_title = ch
                        if toc_map and ch_num in toc_map:
                            ch_title = toc_map[ch_num][0]
                        text = f"{ch_num} {ch_title}"
                    key = _normalize_section_title(text.split("\n")[0].strip())
                    # P1-2 单元1：header 页眉只更新「自己那一层」，不清更深层。
                    # header 是每页重复的章标记（如「1 绪论」），但也是章标题——
                    # 更新一级栈（清掉封面「上册」等），但保留二级栈（不清 1.3），
                    # 避免跨页正文的 section 退化。
                    if mtype == "header":
                        etype = ElementType.HEADING
                        heading_level = inferred_level
                        if heading_level == 1:
                            stack_touched = True
                            # 一级页眉：只更新第 0 层，保留二级（同章跨页不清 1.3）；
                            # 但章号变了要清掉旧章二级（10 渗流 不再挂 9.1）。
                            if not section_stack:
                                section_stack = [key]
                            else:
                                old_chapter = section_stack[0].split()[0] if section_stack[0] else ""
                                new_chapter = key.split()[0] if key else ""
                                if old_chapter and new_chapter and old_chapter != new_chapter:
                                    section_stack = [key]
                                else:
                                    section_stack[0] = key
                        elif heading_level == 2:
                            stack_touched = True
                            # 二级页眉：更新第 1 层，保留一级；章号不符时用 TOC 校正一级
                            new_chapter = key.split(".")[0] if "." in key else key.split()[0]
                            if len(section_stack) >= 1 and section_stack[0].split()[0] != new_chapter:
                                # 二级页眉所属章与一级栈不符：从 TOC 校正一级
                                if toc_map and new_chapter in toc_map:
                                    section_stack[0] = f"{new_chapter} {toc_map[new_chapter][0]}"
                            if len(section_stack) < 1:
                                section_stack = [key]
                            elif len(section_stack) < 2:
                                section_stack.append(key)
                            else:
                                section_stack[1] = key
                    else:
                        etype = ElementType.HEADING
                        heading_level = inferred_level
                        if heading_level in (1, 2):
                            if heading_level == 1 and not stack_touched:
                                section_stack.clear()
                                stack_touched = True
                            if heading_level <= len(section_stack):
                                section_stack = section_stack[: heading_level - 1]
                            section_stack.append(key)
                else:
                    etype = ElementType.HEADING if mtype == "title" else ElementType.PARAGRAPH
                    heading_level = None
        else:
            # 未知类型兜底为段落
            etype = ElementType.PARAGRAPH
            heading_level = None
            table = None

        bbox_raw = raw.get("bbox")
        bbox = tuple(float(v) for v in bbox_raw) if bbox_raw and len(bbox_raw) == 4 else None
        page_idx = raw.get("page_idx")
        page = page_idx + 1 if isinstance(page_idx, int) else None

        flags = frozenset({"layout_model"})
        # P1-2 单元2：段落文本含 LaTeX 标记时统一清理（行内公式 $p_0$、\frac 等）
        # 标题（heading）不含公式，不清理；只清理段落正文里的公式残留
        if etype == ElementType.PARAGRAPH and text and ("$" in text or "\\" in text):
            text = _clean_latex(text)
        # 第一个编号标题之前的内容 → section_path=("目录/前言",)
        if not stack_touched:
            section_path = ("目录/前言",)
        else:
            section_path = tuple(section_stack) if section_stack else ()
        elements.append(
            DocumentElement(
                element_id=f"mineru-{order}",
                type=etype,
                text=text,
                page_start=page,
                page_end=page,
                bbox=bbox,
                reading_order=order,
                heading_level=heading_level,
                section_path=section_path,
                table=table,
                source_ref={
                    "parser": "mineru",
                    "parser_version": (middle or {}).get("_version_name", "unknown"),
                    "block_index": idx,
                },
                flags=flags,
            )
        )
        order += 1
    return elements


def mineru_to_blocks(elements: list["DocumentElement"]) -> list["ParsedBlock"]:
    """IR elements → ParsedBlock（兼容层：保证旧 chunker 链路不破坏）。

    单元 A（图片检索）：figure 保留 block_type="figure"（不再降级 paragraph），
    出处元数据（retriever 契约 text/table/formula/figure）从此真正有 figure 值；
    无图注的图片生成「图 pXX」可检索占位，避免空文本块在切片/合并阶段被丢弃。
    """
    from app.services.parser.base import ParsedBlock

    blocks: list[ParsedBlock] = []
    for el in elements:
        if el.type.value == "heading":
            btype = "heading"
        elif el.type.value == "table":
            btype = "table"
        elif el.type.value == "figure":
            btype = "figure"
            if not el.text.strip():
                # 图片无图注/无识别文字 → 生成可检索占位（含来源页），
                # 让「第X页那张图」类提问仍能定位到图片块。
                # DocumentElement 冻结（不可变），用 replace 生成新对象，不改原 el。
                page = f"p{el.page_start}" if el.page_start else ""
                el = replace(el, text=f"图 {page}".strip())
        else:
            btype = "paragraph"
        section = "/".join(el.section_path) if el.section_path else None
        blocks.append(
            ParsedBlock(text=el.text, section=section, page=el.page_start, block_type=btype)
        )
    return blocks


class MinerUPDFParser:
    """MinerU PDF 解析器（P1-2 单元D 注册用）。parse() = run_mineru → adapt。

    不在 factory 注册（默认关闭）；单元D 决定启用方式。
    """

    extensions: tuple[str, ...] = ("pdf",)

    def parse(self, path: Path, filename: str, chunk_strategy: str = "old", parse_mode: str = "fast"):
        """运行 MinerU → 组装 ParsedDocument（blocks + elements + quality）。

        force=True：每次入库都删除旧产物、强制 MinerU 真正重新解析（保证重灌真实重跑，
        不弄虚作假）。
        parse_mode：单元 S 文档级后端选择。fast=快速（pipeline 老后端）/
        high=高精度（hybrid-engine 新后端）。
        """
        import json

        from app.services.parser.base import ParsedDocument

        backend = "hybrid-engine" if parse_mode == "high" else "pipeline"
        out_dir = settings.data_dir / "mineru_output" / path.stem
        result = run_mineru(path, out_dir, force=True, backend=backend)
        if result["content_list"] is None:
            raise RuntimeError("MinerU 未产出 content_list.json")

        with open(result["content_list"], encoding="utf-8") as f:
            content = json.load(f)
        middle = None
        if result["middle"]:
            with open(result["middle"], encoding="utf-8") as f:
                middle = json.load(f)

        elements = adapt_mineru_output(content, middle, doc_name=filename)
        blocks = mineru_to_blocks(elements)
        quality = {
            "parser": "mineru",
            "engine": "mineru",
            "pages": len({el.page_start for el in elements if el.page_start}),
            "elements": len(elements),
            "blocks": len(blocks),
            "mineru_version": (middle or {}).get("_version_name", "unknown"),
        }
        return ParsedDocument(blocks=blocks, page_count=quality["pages"], quality=quality, elements=elements)
