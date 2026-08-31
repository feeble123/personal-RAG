"""单元 O 全核（第二遍）：逐题反查 note/clauses 指向章节号的实际 section 标题。

只读。对每题：
  - 提取 expect_clauses 条款号 → 找库里含该条款号的 section 标题
  - 提取 note 里的章节号 → 找库里含该编号的 section 标题
  输出「题 | q | clauses→标题 | note章节→标题」，供人工判断 note/clauses 是否张冠李戴。

用法（backend/ 目录）：python -m evaluation.gold_audit2
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import Chunk, Document, KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from evaluation.gold_data import GOLD  # noqa: E402


def _clause_hits(text: str, clause: str) -> bool:
    if not clause:
        return False
    return bool(re.search(rf"(^|[\s/（(]){re.escape(clause)}(?![0-9.])", text))


async def main() -> int:
    async with async_session_factory() as db:
        kbs = (await db.execute(select(KnowledgeBase))).scalars().all()
        kb_by_name = {kb.name: kb.id for kb in kbs}
        rows = (
            await db.execute(
                select(Chunk.kb_id, Chunk.section).join(Document, Chunk.doc_id == Document.id)
                .where(Chunk.document_version_id == Document.active_version_id)
            )
        ).all()
        secs_by_kb: dict[int, list[str]] = defaultdict(list)
        for kid, sec in rows:
            if sec:
                secs_by_kb[kid].append(sec)

        for i, g in enumerate(GOLD, 1):
            kb_id = kb_by_name.get(g["kb"])
            secs = secs_by_kb.get(kb_id, [])
            clause_titles = []
            for cl in g.get("expect_clauses") or []:
                hits = sorted({s for s in secs if _clause_hits(s, cl)})
                clause_titles.append(f"{cl}→{'、'.join(h.split('/')[-1].strip() for h in hits[:3]) or '??'}")
            note = g.get("note", "")
            note_nums = re.findall(r"\b\d+(?:\.\d+)*\b", note)
            note_titles = []
            for num in note_nums:
                if re.fullmatch(r"(19|20)\d{2}", num):
                    continue
                if "." in num or (num.isdigit() and 1 <= len(num) <= 2):
                    hits = sorted({s for s in secs if _clause_hits(s, num)})
                    note_titles.append(f"{num}→{'、'.join(h.split('/')[-1].strip() for h in hits[:3]) or '??'}")

            print(f"[{i:2d}] {g['q'][:30]}")
            print(f"      note={note[:44]}")
            print(f"      clause: {'; '.join(clause_titles) if clause_titles else '(无)'}")
            if note_titles:
                print(f"      note章节: {'; '.join(note_titles)}")
            print(f"      keywords={g.get('expect_keywords')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
