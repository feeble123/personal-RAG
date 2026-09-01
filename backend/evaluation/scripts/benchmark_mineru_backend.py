"""单元 S：MinerU 后端对比（pipeline vs hybrid-engine）。

对同一批真实水利 PDF，两个后端各跑一遍，量化「公式 / 表格 / 旋转页 / 章节层级」四大短板，
产出推荐后端 + 按文档类型路由表。

对比方法：同一页，pipeline 与 hybrid 各解析一次，输出 _content_list.json，比对：
- 公式：content_list 中 equation 类型的数量 + 公式文本是否含 ρ/Σ/∂ 等符号（pipeline 常把 ρ→p）
- 表格：table 类型的块数 + 是否含结构化 HTML 表格（hybrid 图表分析更强）
- 旋转页：文本是否被正确转正（横置页 pipeline 常漏）
- 章节：text_level 标注的标题层级是否合理（一二级大纲）

离线脚本：只读 data/uploads/，中间产物写 data/bakeoff_work/（gitignore）。不碰生产数据。

用法（backend/ 目录）：
    python -m evaluation.scripts.benchmark_mineru_backend --sample 13 --pages 5,6,7
    python -m evaluation.scripts.benchmark_mineru_backend --sample 3 --pages 47,48
"""
from __future__ import annotations

import argparse
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
WORK_DIR = BASE_DIR / "data" / "bakeoff_work"

# stored_path（DB）→ 文件名，doc_id → 代表页（公式/表格/旋转页靶点）
# 页码取自 documents.page_count 与已知短板特征
TARGETS: list[dict] = [
    {
        "doc_id": 3,
        "file": "055515223577_数字孪生水利工程建设技术导则（试行）.pdf",
        "pages": [47, 48],  # 末页横置（旋转页靶点）
        "note": "横置末页（旋转页靶点）",
    },
    {
        "doc_id": 13,
        "file": "47f26ee1649b41498ab1295f59f7c98b.pdf",
        "pages": [24, 25],  # 水力学公式密集页（ρ=m/V 公式靶点）
        "note": "水力学公式密集页（公式靶点）",
    },
    {
        "doc_id": 5,
        "file": "1f8012b2948a4556af3bae852d206e67.pdf",
        "pages": [10, 11],  # 编写规定（含表格）
        "note": "编写规定（表格靶点）",
    },
]


def _extract_page_pdf(pdf_path: Path, page_no: int, work_dir: Path) -> Path:
    """从大 PDF 抽单页成独立 PDF（MinerU 按文件粒度跑）。"""
    import fitz

    one = work_dir / f"{pdf_path.stem}_p{page_no}.pdf"
    if one.exists():
        return one
    src = fitz.open(str(pdf_path))
    doc = fitz.open()
    doc.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
    doc.save(str(one))
    doc.close()
    src.close()
    return one


def _run_backend(backend: str, pdf_path: Path, pages: list[int], work_dir: Path) -> dict:
    """对指定页跑某个 MinerU 后端，返回 {backend, elapsed_s, per_page: {...}}。"""
    # 临时切换 settings.backend（_build_mineru_args 读它）
    settings.mineru_backend = backend
    out_root = work_dir / backend
    out_root.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    per_page: dict[str, dict] = {}
    for pn in pages:
        one = _extract_page_pdf(pdf_path, pn, work_dir)
        out = out_root / f"{pdf_path.stem}_p{pn}"
        out.mkdir(parents=True, exist_ok=True)
        try:
            paths = mineru.run_mineru(one, out)
            per_page[str(pn)] = _analyze_content_list(paths, backend)
        except Exception as exc:  # noqa: BLE001
            logger.error("  [%s] p%d 失败: %s", backend, pn, exc)
            per_page[str(pn)] = {"error": str(exc)[:200]}
    elapsed = time.perf_counter() - start
    return {"backend": backend, "elapsed_s": round(elapsed, 2), "per_page": per_page}


def _analyze_content_list(paths: dict, backend: str) -> dict:
    """解析 _content_list.json，提取公式/表格/章节/旋转页信号。"""
    if not paths.get("content_list"):
        return {"error": "no content_list"}
    with open(paths["content_list"], encoding="utf-8") as f:
        content = json.load(f)

    equations = 0
    equation_symbols = 0
    tables = 0
    table_html = 0
    headings = 0
    heading_levels: list[int] = []
    text_chars = 0
    garble = 0

    # 公式关键符号：pipeline 常把 ρ→p、∑→Σ 误提
    _SYMBOLS = set("ρΣ∂√≤≥∑∫×÷≈π")
    for item in content:
        typ = item.get("type", "")
        txt = item.get("text") or ""
        text_chars += len(txt)
        garble += txt.count("�") + sum(1 for c in txt if 0xE000 <= ord(c) <= 0xF8FF)

        if typ == "equation" or "equation" in str(typ).lower():
            equations += 1
            equation_symbols += sum(1 for c in txt if c in _SYMBOLS)
        if typ == "table" or "table" in str(typ).lower():
            tables += 1
            # 表格是否结构化（HTML <table> 或含 | 分隔）
            if "<table" in txt or "<tr" in txt or "|" in txt:
                table_html += 1
        # 标题层级
        lvl = item.get("text_level") or item.get("level")
        if lvl is not None:
            headings += 1
            try:
                heading_levels.append(int(lvl))
            except (TypeError, ValueError):
                pass

    return {
        "backend": backend,
        "text_chars": text_chars,
        "garble_ratio": round(garble / max(1, text_chars), 4),
        "equations": equations,
        "equation_symbols": equation_symbols,
        "tables": tables,
        "table_structured": table_html,
        "headings": headings,
        "heading_levels": sorted(set(heading_levels)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MinerU 后端对比: pipeline vs hybrid-engine")
    parser.add_argument("--sample", type=int, default=None, help="只跑第 N 个靶点（1 起）")
    parser.add_argument("--backends", type=str, default="pipeline,hybrid-engine", help="后端列表")
    parser.add_argument("--out", type=str, default=str(BASE_DIR / "evaluation" / "mineru_backend_report.json"))
    args = parser.parse_args()

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    targets = [t for i, t in enumerate(TARGETS, start=1) if args.sample is None or i == args.sample]

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    reports: list[dict] = []
    for t in targets:
        pdf_path = UPLOADS / t["file"]
        if not pdf_path.exists():
            logger.warning("样本缺失: %s", pdf_path)
            continue
        logger.info("靶点 doc#%s（%s）页 %s：%s", t["doc_id"], t["file"][:16], t["pages"], t["note"])

        for backend in backends:
            try:
                rep = _run_backend(backend, pdf_path, t["pages"], WORK_DIR)
                rep["doc_id"] = t["doc_id"]
                rep["file"] = t["file"]
                rep["note"] = t["note"]
                reports.append(rep)
            except Exception as exc:  # noqa: BLE001
                logger.error("  [%s] 异常: %s", backend, exc)
                reports.append({"doc_id": t["doc_id"], "file": t["file"], "backend": backend, "error": str(exc)[:200]})

    out = Path(args.out)
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已写: %s", out)

    # 控制台摘要
    print("\n=== 后端对比摘要 ===")
    for r in reports:
        if "error" in r:
            print(f"  {r.get('backend')}: doc#{r.get('doc_id')} → ERROR {r['error']}")
            continue
        print(f"\n  [{r['backend']}] doc#{r['doc_id']} {r['note']} 耗时{r['elapsed_s']}s")
        for pn, pg in sorted(r["per_page"].items(), key=lambda x: int(x[0])):
            if "error" in pg:
                print(f"    p{pn}: ERROR {pg['error']}")
                continue
            print(
                f"    p{pn}: 字符{pg['text_chars']} 乱码{pg['garble_ratio']} "
                f"公式{pg['equations']}(符号{pg['equation_symbols']}) "
                f"表格{pg['tables']}(结构化{pg['table_structured']}) "
                f"标题{pg['headings']} 层级{pg['heading_levels']}"
            )


if __name__ == "__main__":
    main()
