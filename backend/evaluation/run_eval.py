"""P1-1 评测门禁 CLI：跑 gold 集，输出报告 JSON，可对比 baseline。

用法（backend/ 目录）：
    python -m evaluation.run_eval                    # 跑评测，输出 stdout + evaluation/report.json
    python -m evaluation.run_eval --rounds 3         # 跑 3 轮，报均值±方差
    python -m evaluation.run_eval --compare          # 对比 baseline.json，任何 Recall@5 下降则告警
    python -m evaluation.run_eval --save-baseline    # 把当前结果存为 baseline.json（首次/重大改版后）

评测走真实库（data/app.db）+ fake embedding（离线可跑）；rerank 关闭（离线）时
Recall 反映「向量+BM25 融合」基线，改版后同条件对比才有效。

指标：
- 宽松 Recall@5/10（expect_keywords 任一命中）
- 严格条款 Recall@5/10（expect_clauses 条款号命中，无条款的题不计入）
- 分层报告（intent / kb）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# 保证从 backend/ 下运行（evaluation 包可导入）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services import rag  # noqa: E402
from evaluation.gold_data import GOLD  # noqa: E402
from evaluation.scorers import aggregate, has_citation, recall_at_k, recall_clause_at_k  # noqa: E402

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


async def _resolve_kb(db, name: str) -> int | None:
    kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    return kb.id if kb else None


async def _rebuild_bm25(db) -> int:
    """评测前重建全部库 BM25 语料（进程内内存索引，须显式预热才可复现）。

    P1-2 单元2：只保留 active 版本切片。重灌后旧版本标记 retired 但 chunk 行仍留库，
    若连 retired 一起灌进 BM25，评测就会命中旧垃圾切片，指标与生产真实配置（只索引
    active）不一致。
    """
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
    """跑单问：retrieve + 判定命中（宽松关键词 + 严格条款）。"""
    kb_id = await _resolve_kb(db, gold["kb"])
    if kb_id is None:
        return {
            "q": gold["q"], "kb": gold["kb"], "intent": gold["intent"],
            "doc_type": gold.get("doc_type", "other"),
            "kb_found": False, "skipped": True,
            "recall5": False, "recall10": False,
            "clause5": None, "clause10": None, "citation": False,
        }
    try:
        result = await rag.retrieve(
            db, gold["q"], kb_id=kb_id, top_k=top_k, include_snippet=True,
        )
    except Exception as exc:
        return {
            "q": gold["q"], "kb": gold["kb"], "intent": gold["intent"],
            "doc_type": gold.get("doc_type", "other"),
            "kb_found": True, "skipped": True, "error": str(exc)[:120],
            "recall5": False, "recall10": False,
            "clause5": None, "clause10": None, "citation": False,
        }
    cites = result.cites if isinstance(result, rag.RetrievedResult) else result
    clauses = gold.get("expect_clauses")
    return {
        "q": gold["q"], "kb": gold["kb"], "intent": gold["intent"],
        "doc_type": gold.get("doc_type", "other"),
        "kb_found": True, "skipped": False,
        "recall5": recall_at_k(cites, gold["expect_keywords"], 5),
        "recall10": recall_at_k(cites, gold["expect_keywords"], 10),
        "clause5": recall_clause_at_k(cites, clauses, 5),
        "clause10": recall_clause_at_k(cites, clauses, 10),
        "citation": has_citation(cites, 10),
        "top_kb_id": kb_id,
        "hit_keyword": next(
            (kw for kw in gold["expect_keywords"] if any(kw in (c.snippet or "") + " " + (c.section or "") + " " + (c.source or "") for c in cites[:10])),
            None,
        ),
    }


def _print_agg(agg: dict, label: str) -> None:
    print(f"--- {label} ---")
    print(f"有效问数: {agg['total']}")
    print(f"Recall@5 : {agg['recall_at_5']:.1%}")
    print(f"Recall@10: {agg['recall_at_10']:.1%}")
    if agg.get("clause_at_5") is not None:
        print(f"严格条款@5 : {agg['clause_at_5']:.1%}")
        print(f"严格条款@10: {agg['clause_at_10']:.1%}")
    print(f"Citation : {agg['citation_hit']:.1%}")
    print("分意图:")
    for intent, b in agg["by_intent"].items():
        name = intent[0] if isinstance(intent, tuple) else intent
        cl = b.get("clause5")
        cl_str = f"  严格@{cl:.1%}" if cl is not None else ""
        print(f"  {name:12} n={b['n']}  R@5={b['recall5']:.1%}  R@10={b['recall10']:.1%}{cl_str}")
    print("分库:")
    for kb, b in agg["by_kb"].items():
        name = kb[0] if isinstance(kb, tuple) else kb
        print(f"  {str(name)[:30]:32} n={b['n']}  R@5={b['recall5']:.1%}  R@10={b['recall10']:.1%}")


async def main(save_baseline: bool, compare: bool, rounds: int) -> int:
    logging.basicConfig(level=logging.WARNING)
    async with async_session_factory() as db:
        n_kbs = await _rebuild_bm25(db)
        logger.info("评测前 BM25 已重建: %d 个库", n_kbs)

        # 多轮：每轮跑全部 gold，收集 per-round aggregate
        round_aggs: list[dict] = []
        for rd in range(rounds):
            results = []
            for gold in GOLD:
                r = await run_one(db, gold)
                results.append(r)
                if r["skipped"]:
                    if r.get("error"):
                        print(f"⏭  [{r['intent']:10}] {r['q']}  (评测错误: {r['error']})")
                    else:
                        print(f"⏭  [{r['intent']:10}] {r['q']}  (库不存在，跳过)")
                    continue
                mark = "✅" if r["recall5"] else "❌"
                cl = f" 严{r.get('clause5')}" if r.get("clause5") is not None else ""
                print(f"{mark}[{r['intent']:12}] {r['q']}  hit={r['hit_keyword']}{cl}")
            active = [r for r in results if not r["skipped"]]
            agg = aggregate(active)
            round_aggs.append(agg)
            if rounds > 1:
                print(f"\n=== 第 {rd+1} 轮 ===")
                _print_agg(agg, f"第 {rd+1} 轮")

    # 汇总（多轮取均值 + 标准差）
    if rounds == 1:
        agg = round_aggs[0]
        _print_agg(agg, "评测汇总")
    else:
        agg = _avg_aggs(round_aggs)
        print(f"\n=== 汇总（{rounds} 轮均值±std）===")
        print(f"有效问数: {agg['total']}")
        for m in ("recall_at_5", "recall_at_10", "clause_at_5", "clause_at_10", "citation_hit"):
            if agg.get(m) is None:
                continue
            vals = [a.get(m) for a in round_aggs if a.get(m) is not None]
            mean = sum(vals) / len(vals) if vals else 0
            std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5 if vals else 0
            print(f"  {m:14} {mean:.1%} ± {std:.1%}")

    report = {"aggregate": agg, "rounds": round_aggs, "round_count": rounds,
              "skipped": len(GOLD) - agg["total"]}
    report_path = BASE_DIR / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已存: {report_path}")

    if save_baseline:
        (BASE_DIR / "baseline.json").write_text(
            json.dumps({"aggregate": agg}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"baseline 已存: {BASE_DIR / 'baseline.json'}")

    if compare:
        bl_path = BASE_DIR / "baseline.json"
        if not bl_path.exists():
            print("⚠️ 无 baseline.json，请先 --save-baseline", file=sys.stderr)
            return 1
        bl = json.loads(bl_path.read_text(encoding="utf-8"))["aggregate"]
        print("\n===== 与 baseline 对比 =====")
        ok = True
        for metric in ("recall_at_5", "recall_at_10"):
            cur = agg[metric]
            prev = bl[metric]
            delta = cur - prev
            flag = "🟢" if delta >= -0.01 else "🔴"
            print(f"{flag} {metric}: {prev:.1%} → {cur:.1%} ({delta:+.1%})")
            if delta < -0.01:
                ok = False
        print("\n✅ 全部达标" if ok else "\n🔴 有指标下降，需人工审核")
        return 0 if ok else 2
    return 0


def _avg_aggs(aggs: list[dict]) -> dict:
    """多轮 aggregate 取均值（结构同单轮 agg，数值为均值）。"""
    keys = ("total", "recall_at_5", "recall_at_10", "citation_hit", "clause_at_5", "clause_at_10")
    avg: dict = {}
    for k in keys:
        vals = [a.get(k) for a in aggs if a.get(k) is not None]
        avg[k] = round(sum(vals) / len(vals), 4) if vals else (None if k.startswith("clause") else 0)
    avg["by_intent"] = aggs[0]["by_intent"] if aggs else {}
    avg["by_kb"] = aggs[0]["by_kb"] if aggs else {}
    return avg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P1-1 评测门禁")
    parser.add_argument("--save-baseline", action="store_true", help="保存当前结果为 baseline")
    parser.add_argument("--compare", action="store_true", help="对比 baseline 并告警下降")
    parser.add_argument("--rounds", type=int, default=1, help="跑 N 轮（严谨模式用 3）")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.save_baseline, args.compare, args.rounds)))
    except KeyboardInterrupt:
        raise SystemExit(130)
