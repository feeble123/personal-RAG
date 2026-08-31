"""单元 M（评测收尾）诊断：dump 每条模糊题 top-10 检索 chunk，供人工识别真假命中。

只读：真实 PG + 真实 embedding + 真实 rerank，不写任何生产数据。
用法（backend/ 目录）：
    python -m evaluation.vague_diag
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
from evaluation.vague_gold import VAGUE  # noqa: E402

logger = logging.getLogger(__name__)


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
        for gold in VAGUE:
            kb_id = await _resolve_kb(db, gold["kb"])
            print(f"\n{'=' * 78}")
            print(f"[{gold['vague_type']}] {gold['q']}")
            print(f"  kb={gold['kb']}  keywords={gold['expect_keywords']}  clauses={gold['expect_clauses']}")
            if kb_id is None:
                print("  ⚠ 库不存在，跳过")
                continue
            result = await rag.retrieve(db, gold["q"], kb_id=kb_id, top_k=10, include_snippet=True)
            cites = result.cites if isinstance(result, rag.RetrievedResult) else result
            for i, c in enumerate(cites, 1):
                snip = (c.snippet or "").replace("\n", " ")[:80]
                print(f"  {i:2d}. sec=[{c.section}] pg={c.page} src={c.source} | {snip}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
