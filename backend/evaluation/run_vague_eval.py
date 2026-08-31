"""单元 L：模糊问题检索评测入口（独立于 run_eval.py，不碰 60 问逻辑）。

用法（backend/ 目录）：
    python -m evaluation.run_vague_eval

对 vague_gold.py 的 12 问模糊题跑真实检索（真 PG + 真 siliconflow embedding + 真 rerank），
输出 Recall@5/10 + 严格条款 + 分模糊类型（A 隐式指代 / B 词过泛 / C 口语换说法）。
判定复用 evaluation/scorers.py，规则与 60 问完全一致，便于直接对比。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services import rag  # noqa: E402
from evaluation.scorers import aggregate, has_citation, recall_at_k, recall_clause_at_k  # noqa: E402
from evaluation.vague_gold import VAGUE  # noqa: E402

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


async def _resolve_kb(db, name: str) -> int | None:
    kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    return kb.id if kb else None


async def _rebuild_bm25(db) -> int:
    """评测前重建全部库 BM25 语料（只索引 active 版本，与生产一致）。"""
    from app.services import bm25
    from app.db.models import Chunk, Document

    rows = (
        await db.execute(
            select(Chunk.kb_id, Chunk.id, Chunk.content)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.document_version_id == Document.active_version_id)
        )
    ).all()
    grouped: dict[int, list[tuple[int, str]]] = {}
    for kid, cid, content in rows:
        grouped.setdefault(kid, []).append((cid, content))
    for kid, items in grouped.items():
        await asyncio.to_thread(bm25.rebuild, kid, items)
    return len(grouped)


async def run_one(db, gold: dict, top_k: int = 10) -> dict:
    """跑单问：retrieve + 判定（与 run_eval 的 run_one 同口径）。"""
    kb_id = await _resolve_kb(db, gold["kb"])
    if kb_id is None:
        return {
            "q": gold["q"], "kb": gold["kb"], "vague_type": gold.get("vague_type"),
            "kb_found": False, "skipped": True,
            "recall5": False, "recall10": False, "clause5": None, "clause10": None,
        }
    try:
        result = await rag.retrieve(db, gold["q"], kb_id=kb_id, top_k=top_k, include_snippet=True)
    except Exception as exc:
        return {
            "q": gold["q"], "kb": gold["kb"], "vague_type": gold.get("vague_type"),
            "kb_found": True, "skipped": True, "error": str(exc)[:120],
            "recall5": False, "recall10": False, "clause5": None, "clause10": None,
        }
    cites = result.cites if isinstance(result, rag.RetrievedResult) else result
    clauses = gold.get("expect_clauses")
    return {
        "q": gold["q"], "kb": gold["kb"], "vague_type": gold.get("vague_type"),
        "kb_found": True, "skipped": False,
        "recall5": recall_at_k(cites, gold["expect_keywords"], 5),
        "recall10": recall_at_k(cites, gold["expect_keywords"], 10),
        "clause5": recall_clause_at_k(cites, clauses, 5),
        "clause10": recall_clause_at_k(cites, clauses, 10),
        "citation": has_citation(cites, 10),
        "hit_keyword": next(
            (kw for kw in gold["expect_keywords"] if any(
                kw in (c.snippet or "") + " " + (c.section or "") + " " + (c.source or "")
                for c in cites[:10]
            )),
            None,
        ),
    }


def _print_by_type(results: list[dict]) -> None:
    """按模糊类型 A/B/C 分组报 Recall（关键词宽松 + 严格条款两口径）。"""
    print("\n--- 分模糊类型 ---")
    for t in ("A", "B", "C"):
        rows = [r for r in results if r.get("vague_type") == t]
        if not rows:
            continue
        n = len(rows)
        r5 = sum(1 for r in rows if r["recall5"]) / n
        r10 = sum(1 for r in rows if r["recall10"]) / n
        cl_rows = [r for r in rows if r.get("clause5") is not None]
        cl5 = sum(1 for r in cl_rows if r["clause5"]) / len(cl_rows) if cl_rows else None
        name = {"A": "隐式指代/省略", "B": "词过泛/缺主语", "C": "口语换说法"}.get(t, t)
        cl_s = f"  严格条款@5={cl5:.1%}(n={len(cl_rows)})" if cl5 is not None else ""
        print(f"  {t} {name:14} n={n}  关键词R@5={r5:.1%} R@10={r10:.1%}{cl_s}")


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    async with async_session_factory() as db:
        n_kbs = await _rebuild_bm25(db)
        logger.info("评测前 BM25 已重建: %d 个库", n_kbs)

        results = []
        for gold in VAGUE:
            r = await run_one(db, gold)
            results.append(r)
            if r["skipped"]:
                print(f"⏭  [{r.get('vague_type')}] {r['q']}  ({r.get('error', '库不存在')})")
                continue
            cl = f"{'✅' if r['clause5'] else '❌'}" if r.get("clause5") is not None else "无锚点"
            kw = "✅" if r["recall5"] else "❌"
            # 主标记：有锚点看严格条款，无锚点退回关键词口径
            mark = (f"{'✅' if r['clause5'] else '❌'}" if r.get("clause5") is not None
                    else kw)
            print(f"{mark}[{r['vague_type']}] {r['q']}  词@5={kw} 章@5={cl}")

    active = [r for r in results if not r["skipped"]]
    if not active:
        print("无有效评测（全 skipped）")
        return 0

    agg = aggregate(active)
    print("\n===== 模糊问题评测汇总 =====")
    print(f"有效问数: {agg['total']}")
    print("\n【可信数字·严格口径】正确章节进 top-k（不受高频词干扰）")
    if agg.get("clause_at_5") is not None:
        print(f"  严格条款 Recall@5 : {agg['clause_at_5']:.1%}")
        print(f"  严格条款 Recall@10: {agg['clause_at_10']:.1%}")
    print("\n【宽松参考·关键词口径】top-k 任一 chunk 命中任一关键词")
    print(f"  关键词 Recall@5 : {agg['recall_at_5']:.1%}")
    print(f"  关键词 Recall@10: {agg['recall_at_10']:.1%}")
    _print_by_type(active)

    report = {"aggregate": agg, "results": active}
    (BASE_DIR / "vague_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n报告已存: {BASE_DIR / 'vague_report.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
