"""B2 验证实验：抽「水资源规划及利用」第45页（乱码公式页）跑 hybrid-engine，
对比 pipeline vs hybrid 的公式识别质量，验证混合重灌能否修好公式乱码。

离线脚本：只读 data/uploads/，中间产物写 data/hybrid_verify/（gitignore 不碰）。
不碰生产数据。

用法（backend/ 目录）：
    python -m evaluation.scripts.benchmark_hybrid_formula_page
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

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
UPLOADS = BASE_DIR / "data" / "uploads"
WORK = BASE_DIR / "data" / "hybrid_verify"

# 文档14 = 水资源规划及利用，源文件 stored_path 的 stem
PDF = UPLOADS / "25389f46adcd40dd95d7c0e47d5497d3.pdf"
PAGE = 45  # page_idx（0-based），= 第46页，用户举例的乱码公式页


def _extract_page(pdf_path: Path, page_idx: int, work: Path) -> Path:
    """从大 PDF 抽单页成独立 PDF（MinerU 按文件粒度跑）。"""
    import fitz

    one = work / f"p{page_idx}.pdf"
    if one.exists():
        return one
    src = fitz.open(str(pdf_path))
    doc = fitz.open()
    doc.insert_pdf(src, from_page=page_idx, to_page=page_idx)
    doc.save(str(one))
    doc.close()
    src.close()
    return one


def _dump_formulas(paths: dict, label: str) -> list[str]:
    """提取公式块（equation type + 含 LaTeX 的 text），返回可读文本行。"""
    lines: list[str] = []
    if not paths.get("content_list"):
        lines.append(f"[{label}] 无 content_list")
        return lines
    with open(paths["content_list"], encoding="utf-8") as f:
        content = json.load(f)
    lines.append(f"[{label}] 共 {len(content)} 块，公式相关：")
    for item in content:
        typ = str(item.get("type", ""))
        txt = item.get("text") or ""
        if isinstance(txt, list):
            txt = " ".join(txt)
        if typ == "equation" or "$" in txt or "\\" in txt:
            lines.append(f"  type={typ} | {txt.strip()[:160]!r}")
    return lines


def main() -> None:
    if not PDF.exists():
        logger.error("源 PDF 缺失: %s", PDF)
        return
    WORK.mkdir(parents=True, exist_ok=True)
    one = _extract_page(PDF, PAGE, WORK)

    report: list[str] = []
    for backend in ("pipeline", "hybrid-engine"):
        out = WORK / backend
        out.mkdir(parents=True, exist_ok=True)
        logger.info("===== %s p%d 开始 =====", backend, PAGE)
        start = time.perf_counter()
        try:
            paths = mineru.run_mineru(one, out, force=True, backend=backend)
            elapsed = round(time.perf_counter() - start, 1)
            report.append(f"\n===== {backend} 耗时 {elapsed}s =====")
            report.extend(_dump_formulas(paths, backend))
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.perf_counter() - start, 1)
            report.append(f"\n===== {backend} 失败({elapsed}s): {exc} =====")

    out_path = WORK / "report.txt"
    out_path.write_text("\n".join(report), encoding="utf-8")
    print(f"\n报告已写: {out_path}")


if __name__ == "__main__":
    main()
