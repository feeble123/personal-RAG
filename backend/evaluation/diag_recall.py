"""单元 P 诊断：逐题回放 7 个召回短板，定位「正确章节没进前5」的断点。

只读：连真实 PG + 真实 rerank（与 run_eval 同配置）。
对每题输出：
  1. 检索 top-10 实际命中（section / clause_no / 前40字）
  2. 目标条款号对应的库内 chunk（存在性 + 内容前80字）——确认「答案在库、只是没捞到」
  3. 目标 chunk 在候选池的 trace 得分（vector / bm25 / fusion / rerank）与候选排名

用法（backend/ 目录）：python -m evaluation.diag_recall
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import Chunk, Document, KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services import rag  # noqa: E402

# 单元 P 的 7 个召回短板（题 + 库 + 目标条款）
TARGETS = [
    ("数字孪生流域建设的数据底板包括哪些内容？", "数字孪生水利导则", ["6.1"]),
    ("数字孪生平台的数据底板包括哪些？", "数字孪生水利导则", ["6.1"]),
    ("水利技术标准编写规定中，引用标准的编写要求是什么？", "水利技术标准编写规定", ["3.2"]),
    ("应急预案中的应急响应分为几级？", "重庆市防汛抗旱应急预案", ["4.1"]),
    ("预案中技术保障有哪些内容？", "重庆市防汛抗旱应急预案", ["5.11"]),
    ("预警信息分为哪几个等级？", "重庆市防汛抗旱应急预案", ["3.4"]),
    ("液体运动的流束理论包括哪些内容？", "水力学第5版", ["3"]),
]


def _clause_hits(text: str, clause: str) -> bool:
    if not clause:
        return False
    if clause.isdigit():
        return bool(re.search(rf"(^|[\s/（(]){re.escape(clause)}(?![0-9])", text))
    return bool(re.search(rf"(^|[\s/（(]){re.escape(clause)}(?![0-9.])", text))


async def _rebuild_bm25(db) -> int:
    """与 run_eval 同款：评测前重建 BM25（进程内内存索引，须显式预热）。"""
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


async def main() -> int:
    async with async_session_factory() as db:
        await _rebuild_bm25(db)
        kb_by_name = {
            kb.name: kb.id
            for kb in (await db.execute(select(KnowledgeBase))).scalars().all()
        }
        for q, kb_name, clauses in TARGETS:
            kb_id = kb_by_name.get(kb_name)
            if kb_id is None:
                print(f"\n❌ 库不存在: {kb_name}  ← {q}")
                continue
            print(f"\n{'=' * 70}\n题：{q}\n库：{kb_name}  目标条款：{clauses}")

            result = await rag.retrieve(
                db, q, kb_id=kb_id, top_k=10, include_snippet=True, return_trace=True
            )
            cites = result.cites
            trace = result.trace

            print(f"\n[top-{len(cites)} 实际命中]（expanded={trace.expanded_type} rerank={trace.rerank_status}）")
            for c in cites:
                print(f"  #{c.rank} {c.section or '-'} | clause={c.clause_no or '-'} | {c.snippet[:36].replace(chr(10), ' ')}")

            # 目标 chunk 在库内的真实情况
            rows = (
                await db.execute(
                    select(Chunk, Document)
                    .join(Document, Chunk.doc_id == Document.id)
                    .where(Chunk.kb_id == kb_id, Document.active_version_id.is_not(None))
                )
            ).all()
            target_hits = [
                (c, d)
                for c, d in rows
                if c.document_version_id == d.active_version_id
                and any(_clause_hits((c.section or "") + " " + (c.clause_no or ""), cl) for cl in clauses)
            ]
            print(f"\n[库内命中目标条款的 chunk 数] {len(target_hits)}")
            cand_map = {tc.chunk_id: tc for tc in trace.candidates} if trace else {}
            ranked_cids = [c.chunk_id for c in cites]
            for c, d in target_hits[:6]:
                tc = cand_map.get(c.id)
                in_top = c.id in ranked_cids
                line = (
                    f"  chunk#{c.id} {c.section or '-'} | clause={c.clause_no or '-'} | "
                    f"len={len(c.content or '')} | 内容:{(c.content or '')[:60].replace(chr(10), ' ')}"
                )
                if tc:
                    line += f"\n        trace: vec={tc.vector_score} bm25={tc.bm25_score} fuse={tc.fusion_score} rerank={tc.rerank_score}"
                else:
                    line += "\n        trace: ⚠️ 不在候选池（向量+BM25 都没捞到）"
                line += f"  {'✅top10' if in_top else '❌未进top10'}"
                print(line)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
