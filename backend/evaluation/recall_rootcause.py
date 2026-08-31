"""单元 N 诊断：定位模糊题召回短板根因（正确块在哪一环掉出）。

对每个失败问题：
  1. focus_rerank_query 提取了什么聚焦词
  2. return_trace 跑 retrieve，看正确块（按 section 关键词匹配）的
     vector_score / bm25_score / fusion_score / rerank_score
  3. 最终 top-k 的 section 列表

只读：真实 PG + 真实 embedding + 真实 rerank，不写生产数据。
用法（backend/ 目录）：python -m evaluation.recall_rootcause
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import Chunk, Document, KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services import rag  # noqa: E402

logger = logging.getLogger(__name__)

# 目标问题：(问题, 库名, 正确块的 section 关键词, 正确块内容关键词)
TARGETS = [
    (
        "农村供水的工程要建哪些东西？",
        "数字孪生农村供水工程建设技术指南（试行）",
        "2.2", "系统组成",
    ),
    (
        "山坡上的土石要往下滑，怎么看它稳不稳？",
        "GB 38509-2020 滑坡防治设计规范",
        "7", "稳定性",
    ),
    (
        "水在管子里流，什么时候可以直接按长管算？",
        "水力学第5版",
        "5.1", "长管",
    ),
]


async def _resolve_kb(db, name: str) -> int | None:
    kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    return kb.id if kb else None


async def _rebuild_bm25(db) -> int:
    from app.services import bm25

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
    logging.basicConfig(level=logging.WARNING)
    async with async_session_factory() as db:
        await _rebuild_bm25(db)
        for query, kb_name, sec_kw, content_kw in TARGETS:
            kb_id = await _resolve_kb(db, kb_name)
            print(f"\n{'=' * 80}")
            print(f"Q: {query}")
            print(f"  focus_rerank_query = {rag.focus_rerank_query(query)!r}")
            if kb_id is None:
                print(f"  ⚠ 库不存在: {kb_name}")
                continue
            result = await rag.retrieve(
                db, query, kb_id=kb_id, top_k=10, include_snippet=True, return_trace=True
            )
            trace = result.trace
            print(f"  rerank_status={trace.rerank_status}  vector_hits={trace.vector_hits}  bm25_hits={trace.bm25_hits}")

            # 从 trace 候选里找正确块
            print(f"  --- 正确块（section 含 {sec_kw!r} 且 content 含 {content_kw!r}）在候选池的分数轨迹 ---")
            # 直接查 DB 里符合 section 的 chunk，再对照 trace
            rows = (
                await db.execute(
                    select(Chunk.id, Chunk.section, Chunk.content).where(
                        Chunk.kb_id == kb_id,
                        Chunk.section.ilike(f"%{sec_kw}%"),
                    )
                )
            ).all()
            # 找同时含 content_kw 的
            matched = [(cid, sec, content) for cid, sec, content in rows
                       if content_kw in (content or "") and (sec or "")]
            if not matched:
                matched = [(cid, sec, content) for cid, sec, content in rows if sec]
            for cid, sec, content in matched[:6]:
                ct = trace.candidates
                trace_c = next((c for c in ct if c.chunk_id == cid), None)
                if trace_c:
                    found = True
                    print(
                        f"    chunk={cid} sec=[{sec[:40]}] "
                        f"vec={trace_c.vector_score:.3f} bm25={trace_c.bm25_score} "
                        f"fusion={trace_c.fusion_score:.4f} rerank={trace_c.rerank_score}"
                    )
                else:
                    print(f"    chunk={cid} sec=[{sec[:40]}]  ❌ 不在候选池（向量+BM25 都没召进）")
            if not matched:
                print(f"    ⚠ 库里没有 section 含 {sec_kw!r} 的 chunk")

            # 最终 top-k
            print(f"  --- 最终 top-{len(result.cites)} ---")
            for i, c in enumerate(result.cites, 1):
                print(f"    {i:2d}. [{c.section}] pg={c.page} | {(c.snippet or '')[:60]}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
