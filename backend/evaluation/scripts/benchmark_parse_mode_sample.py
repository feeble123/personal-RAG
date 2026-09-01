"""单元 S 冒烟：对「水资源规划及利用」扫描教材样本，两种解析模式各跑一遍对比。

fast=快速（pipeline 老后端） vs high=高精度（hybrid-engine 新后端）。

对比：耗时 / 每页识别字符数 / 公式 / 表格 / 标题层级 / 乱码率。
样本：E:\\GPT-Codex\\LangChainRAG\\PDF test\\_sample_16p.pdf（前 16 页，纯扫描件）。

用法（backend/ 目录）：
    python -m evaluation.scripts.benchmark_parse_mode_sample
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.core.config import settings  # noqa: E402
from app.services.parser import mineru  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)

SAMPLE = Path(r"E:\GPT-Codex\LangChainRAG\PDF test\_sample_mid20.pdf")
WORK = Path(__file__).resolve().parents[2] / "data" / "parse_mode_smoke"

# parse_mode → mineru backend
_MODE_BACKEND = {"fast": "pipeline", "high": "hybrid-engine"}


def _analyze(content_list_path: Path) -> dict:
    with open(content_list_path, encoding="utf-8") as f:
        content = json.load(f)
    text_chars = 0
    garble = 0
    equations = 0
    eq_symbols = 0
    tables = 0
    table_structured = 0
    headings = 0
    heading_levels: set[int] = set()
    _SYMBOLS = set("ρΣ∂√≤≥∑∫×÷≈π")
    for item in content:
        typ = str(item.get("type", ""))
        txt = item.get("text") or ""
        text_chars += len(txt)
        garble += txt.count("�") + sum(1 for c in txt if 0xE000 <= ord(c) <= 0xF8FF)
        if "equation" in typ.lower():
            equations += 1
            eq_symbols += sum(1 for c in txt if c in _SYMBOLS)
        if "table" in typ.lower():
            tables += 1
            if "<table" in txt or "<tr" in txt or "|" in txt:
                table_structured += 1
        lvl = item.get("text_level") if item.get("text_level") is not None else item.get("level")
        if lvl is not None:
            headings += 1
            try:
                heading_levels.add(int(lvl))
            except (TypeError, ValueError):
                pass
    return {
        "text_chars": text_chars,
        "garble_ratio": round(garble / max(1, text_chars), 4),
        "equations": equations,
        "equation_symbols": eq_symbols,
        "tables": tables,
        "table_structured": table_structured,
        "headings": headings,
        "heading_levels": sorted(heading_levels),
    }


def main() -> None:
    if not SAMPLE.exists():
        logger.error("样本缺失: %s", SAMPLE)
        return

    report: dict = {}
    for mode, backend in _MODE_BACKEND.items():
        out_dir = WORK / mode
        out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("===== %s（%s）开始 =====", mode, backend)
        start = time.perf_counter()
        try:
            paths = mineru.run_mineru(SAMPLE, out_dir, force=True, backend=backend)
            elapsed = round(time.perf_counter() - start, 1)
            if not paths.get("content_list"):
                logger.error("[%s] 无 content_list 产物", mode)
                report[mode] = {"backend": backend, "elapsed_s": elapsed, "error": "no content_list"}
                continue
            stats = _analyze(paths["content_list"])
            stats["backend"] = backend
            stats["elapsed_s"] = elapsed
            report[mode] = stats
            logger.info("[%s] 完成：%s", mode, json.dumps(stats, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.perf_counter() - start, 1)
            logger.error("[%s] 失败: %s", mode, exc)
            report[mode] = {"backend": backend, "elapsed_s": elapsed, "error": str(exc)[:200]}

    out_path = WORK / "report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 解析模式对比摘要 ===")
    for mode in ("fast", "high"):
        r = report.get(mode, {})
        if "error" in r:
            print(f"  {mode}: ERROR {r.get('error')}")
            continue
        print(
            f"  [{mode}] {r.get('backend')} 耗时{r.get('elapsed_s')}s | "
            f"字符{r.get('text_chars')} 乱码{r.get('garble_ratio')} | "
            f"公式{r.get('equations')}(符号{r.get('equation_symbols')}) | "
            f"表格{r.get('tables')}(结构化{r.get('table_structured')}) | "
            f"标题{r.get('headings')} 层级{r.get('heading_levels')}"
        )
    print(f"\n报告: {out_path}")


if __name__ == "__main__":
    main()
