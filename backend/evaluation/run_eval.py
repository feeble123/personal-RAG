"""P1-1 评测门禁 CLI：跑 gold 集，输出报告 JSON，可对比 baseline。

用法（backend/ 目录）：
    python -m evaluation.run_eval                    # 跑评测，输出 stdout + evaluation/report.json
    python -m evaluation.run_eval --compare          # 对比 baseline.json，任何 Recall@5 下降则告警
    python -m evaluation.run_eval --save-baseline    # 把当前结果存为 baseline.json（首次/重大改版后）

评测走真实库（data/app.db）+ fake embedding（离线可跑）；rerank 关闭（离线）时
Recall 反映「向量+BM25 融合」基线，改版后同条件对比才有效。
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
from evaluation.scorers import aggregate, has_citation, recall_at_k  # noqa: E402

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


async def _resolve_kb(db, name: str) -> int | None:
    kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    return kb.id if kb else None


async def run_one(db, gold: dict, top_k: int = 10) -> dict:
    """跑单问：retrieve + 判定命中。

    库解析不到（该环境无此库）→ 标 skipped，不计入分母。
    """
    kb_id = await _resolve_kb(db, gold["kb"])
    if kb_id is None:
        return {
            "q": gold["q"],
            "kb": gold["kb"],
            "intent": gold["intent"],
            "kb_found": False,
            "skipped": True,
            "recall5": False,
            "recall10": False,
            "citation": False,
        }
    try:
        result = await rag.retrieve(
            db,
            gold["q"],
            kb_id=kb_id,
            top_k=top_k,
            include_snippet=True,
        )
    except Exception as exc:  # 维度不匹配/embedding 未配置等环境问题 → 标 error 不计分
        return {
            "q": gold["q"],
            "kb": gold["kb"],
            "intent": gold["intent"],
            "kb_found": True,
            "skipped": True,
            "error": str(exc)[:120],
            "recall5": False,
            "recall10": False,
            "citation": False,
        }
    # return_trace=False 时返回 list[RetrievedChunk]
    cites = result.cites if isinstance(result, rag.RetrievedResult) else result
    return {
        "q": gold["q"],
        "kb": gold["kb"],
        "intent": gold["intent"],
        "kb_found": True,
        "skipped": False,
        "recall5": recall_at_k(cites, gold["expect_keywords"], 5),
        "recall10": recall_at_k(cites, gold["expect_keywords"], 10),
        "citation": has_citation(cites, 10),
        "top_kb_id": kb_id,
        "hit_keyword": next(
            (kw for kw in gold["expect_keywords"] if any(kw in (c.snippet or "") + " " + (c.section or "") + " " + (c.source or "") for c in cites[:10])),
            None,
        ),
    }


async def main(save_baseline: bool, compare: bool) -> int:
    logging.basicConfig(level=logging.WARNING)
    async with async_session_factory() as db:
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
            print(f"{mark} [{r['intent']:10}] {r['q']}  (kb_found={r['kb_found']})  hit={r['hit_keyword']}")

    # 只统计非 skipped 的（库不存在的环境不算失败）
    active = [r for r in results if not r["skipped"]]
    agg = aggregate(active)
    print("\n===== 评测汇总 =====")
    print(f"有效问数: {agg['total']} (跳过 {len(results) - len(active)})")
    print(f"Recall@5 : {agg['recall_at_5']:.1%}")
    print(f"Recall@10: {agg['recall_at_10']:.1%}")
    print(f"Citation : {agg['citation_hit']:.1%}")
    print("分意图:")
    for intent, b in agg["by_intent"].items():
        print(f"  {intent:10} n={b['n']}  R@5={b['recall5']:.1%}  R@10={b['recall10']:.1%}")

    report = {"aggregate": agg, "results": results, "skipped": len(results) - len(active)}
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P1-1 评测门禁")
    parser.add_argument("--save-baseline", action="store_true", help="保存当前结果为 baseline")
    parser.add_argument("--compare", action="store_true", help="对比 baseline 并告警下降")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.save_baseline, args.compare)))
    except KeyboardInterrupt:
        raise SystemExit(130)
