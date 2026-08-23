"""P1-2 单元B：bake-off harness —— RapidOCR vs MinerU 真实指标对比。

对同一批真实扫描 PDF，两个解析引擎各跑一遍，记录耗时/内存/字符量/乱码率/
条款号召回/文字层 GT 相似率，输出加权评分 + 按文档类型路由表。

离线脚本：只读 data/uploads/ 样本，输出 evaluation/report.json（保留进 git），
中间渲染 PNG 写 data/bakeoff_cache/（gitignore）。不碰 ingestion 生产数据。

用法（backend/ 目录）：
    python -m evaluation.scripts.benchmark_parsers --pages-per-doc 3
    python -m evaluation.scripts.benchmark_parsers --engines rapid,text --pages-per-doc 5
    python -m evaluation.scripts.benchmark_parsers --sample 1 --pages-per-doc 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.services.parser import mineru  # noqa: E402
from app.services.parser.pdf import PDFParser  # noqa: E402
from evaluation.scripts.benchmark_metrics import (  # noqa: E402
    assert_same_sample_pages,
    build_route_table,
    compute_metrics,
    weighted_score,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
UPLOADS = BASE_DIR / "data" / "uploads"
CACHE_DIR = BASE_DIR / "data" / "bakeoff_cache"

# 样本清单：(doc_type, 文件名, 建议页数)
SAMPLES: list[dict] = [
    {"doc_type": "scanned_standard", "file": "055515223577_数字孪生水利工程建设技术导则（试行）.pdf", "pages": None, "note": "48页全扫描，含横置末页"},
    {"doc_type": "scanned_standard", "file": "1f8012b2948a4556af3bae852d206e67.pdf", "pages": None, "note": "54页全扫描"},
    {"doc_type": "scanned_standard", "file": "f8324b3bcb324077acf88bf44bc1261a.pdf", "pages": None, "note": "32页全扫描"},
    {"doc_type": "text_layer", "file": "shuili.pdf", "pages": None, "note": "文字层对照（tests/data）"},
]


def _sample_path(s: dict) -> Path:
    if s["file"] == "shuili.pdf":
        return BASE_DIR / "tests" / "data" / "shuili.pdf"
    return UPLOADS / s["file"]


def _render_page_cache(pdf_path: Path, page_no: int, dpi: int = 200) -> Path:
    """渲染页面 PNG 到缓存（两引擎共用，避免重复渲染）。"""
    import fitz

    cache_dir = CACHE_DIR / pdf_path.stem
    cache_dir.mkdir(parents=True, exist_ok=True)
    png = cache_dir / f"p{page_no}_{dpi}.png"
    if not png.exists():
        doc = fitz.open(str(pdf_path))
        page = doc[page_no - 1]
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(str(png))
        doc.close()
    return png


def _run_rapid(pdf_path: Path, pages: list[int]) -> dict:
    """RapidOCR 路径：用 PDFParser.parse 对选定页跑（只保留这些页的文本）。"""
    parser = PDFParser()
    parsed = parser.parse(pdf_path, pdf_path.name, chunk_strategy="old")
    # 只取选定页的块文本（按 page 过滤）
    texts = [b.text for b in parsed.blocks if b.page in pages]
    return {
        "text": "\n".join(texts),
        "quality": dict(parsed.quality),
        "elapsed_s": None,  # 外部计时
    }


def _run_mineru(pdf_path: Path, pages: list[int], work_dir: Path) -> dict:
    """MinerU 路径：对选定页跑（子进程），拼各页 content_list 文本。"""
    import json

    results = []
    for pn in pages:
        # 逐页单页 PDF（MinerU 按文件粒度跑）
        import fitz

        one = work_dir / f"{pdf_path.stem}_p{pn}.pdf"
        src = fitz.open(str(pdf_path))
        doc = fitz.open()
        doc.insert_pdf(src, from_page=pn - 1, to_page=pn - 1)
        doc.save(str(one))
        doc.close()
        src.close()

        out = work_dir / f"out_{pdf_path.stem}_p{pn}"
        mineru.run_mineru(one, out)
        paths = mineru.output_paths(out)
        if paths["content_list"]:
            with open(paths["content_list"], encoding="utf-8") as f:
                content = json.load(f)
            for item in content:
                t = item.get("text", "")
                if t.strip():
                    results.append(t)
    return {"text": "\n".join(results), "quality": {}, "elapsed_s": None}


def measure(engine_name: str, sample: dict, pages: list[int], work_dir: Path) -> dict:
    """跑单个引擎，记录耗时/内存，返回报告条目。"""
    pdf_path = _sample_path(sample)
    if not pdf_path.exists():
        return {"doc": sample["file"], "engine": engine_name, "error": "sample missing"}

    start = time.perf_counter()
    if engine_name == "mineru":
        res = _run_mineru(pdf_path, pages, work_dir)
    elif engine_name == "rapid":
        res = _run_rapid(pdf_path, pages)
    else:  # text = 文字层 GT（仅对照，不算引擎）
        import fitz

        doc = fitz.open(str(pdf_path))
        texts = []
        for pn in pages:
            texts.append(doc[pn - 1].get_text("text"))
        doc.close()
        res = {"text": "\n".join(texts), "quality": {}, "elapsed_s": None}
    elapsed = time.perf_counter() - start

    gt_text = None
    if sample["doc_type"] == "text_layer":
        import fitz

        doc = fitz.open(str(pdf_path))
        gt_text = "\n".join(doc[pn - 1].get_text("text") for pn in pages)
        doc.close()

    metrics = compute_metrics(res["text"], ground_truth=gt_text)
    score = weighted_score(metrics, time_cost_s=elapsed)
    report = {
        "doc": sample["file"],
        "doc_type": sample["doc_type"],
        "engine": engine_name,
        "pages": pages,
        "page_count": len(pages),
        "elapsed_s": round(elapsed, 2),
        "total_chars": metrics["total_chars"],
        "garble_ratio": metrics["garble_ratio"],
        "chinese_common_ratio": metrics["chinese_common_ratio"],
        "clause_count": metrics["clause_count"],
        "gt_similarity": metrics.get("gt_similarity"),
        "score": score,
    }
    logger.info(
        "  [%s] %s %d页 %ss score=%s",
        engine_name, sample["file"][:20], len(pages), round(elapsed, 1), score,
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="bake-off: RapidOCR vs MinerU")
    parser.add_argument("--sample", type=int, default=None, help="只跑第 N 个样本（1 起）")
    parser.add_argument("--pages-per-doc", type=int, default=3, help="每文档页数（默认 3，防 30 分钟）")
    parser.add_argument("--engines", type=str, default="rapid,mineru", help="引擎列表，逗号分隔")
    parser.add_argument("--out", type=str, default=str(BASE_DIR / "evaluation" / "parser_bakeoff_report.json"))
    args = parser.parse_args()

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    samples = [s for i, s in enumerate(SAMPLES, start=1) if args.sample is None or i == args.sample]

    work_dir = BASE_DIR / "data" / "bakeoff_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    reports: list[dict] = []
    for sample in samples:
        pdf_path = _sample_path(sample)
        if not pdf_path.exists():
            logger.warning("样本缺失: %s", pdf_path)
            continue
        import fitz

        doc = fitz.open(str(pdf_path))
        total = doc.page_count
        doc.close()
        n = min(args.pages_per_doc, total)
        # 取前 n 页；若文档含横置末页，加上末页
        pages = list(range(1, n + 1))
        if "横置" in sample.get("note", "") and total > n:
            pages.append(total)  # 末页（横置）
        logger.info("样本 %s（%d页，取 %s）", sample["file"][:20], total, pages)

        for engine in engines:
            try:
                rep = measure(engine, sample, pages, work_dir)
                if "error" in rep:
                    logger.warning("  [%s] 失败: %s", engine, rep["error"])
                reports.append(rep)
            except Exception as exc:  # noqa: BLE001
                logger.error("  [%s] 异常: %s", engine, exc)
                reports.append({"doc": sample["file"], "engine": engine, "error": str(exc)[:200]})

    # 页集一致性校验（rapid 与 mineru 必须跑相同页）
    try:
        assert_same_sample_pages(reports)
    except ValueError as exc:
        logger.warning("页集不一致: %s", exc)

    route_table = build_route_table(reports)
    result = {
        "meta": {"engines": engines, "pages_per_doc": args.pages_per_doc},
        "reports": reports,
        "route_table": route_table,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已写: %s", out)

    # 控制台摘要
    print("\n=== 路由表（推荐引擎）===")
    for doc_type, rec in route_table.items():
        print(f"  {doc_type}: {rec['recommended']} (score={rec['score']}, {rec['pages']}页, {rec['est_time_s']}s)")
    print("\n=== 各引擎评分 ===")
    for r in reports:
        if "error" in r:
            print(f"  {r['engine']}: {r['doc'][:20]} → ERROR {r['error']}")
        else:
            print(f"  {r['engine']}: {r['doc'][:20]} {r['page_count']}页 score={r['score']} 乱码={r['garble_ratio']} 条款={r['clause_count']}")


if __name__ == "__main__":
    main()
