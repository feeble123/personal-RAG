"""单元 O 全核诊断：逐题连库核对 gold 集标注是否可信。

只读：连真实 PG，加载每个库的 active chunks，核对每题：
  1. expect_clauses 条款号 → 库内 section 能否命中（用 scorers 同款边界匹配）
  2. expect_keywords 关键词 → 库内 content/section 能否命中
  3. note 里写的章节号 → 库内 section 是否存在

输出三类信号：
  ❌ 条款号找不到 / 关键词找不到（= 答案不在库 或 标注标错，须人工深挖）
  ⚠️ note 章节号与条款号所在章节不一致（= note 标错）
  ✅ 全过

用法（backend/ 目录）：python -m evaluation.gold_audit
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
    """与 scorers.chunk_hits_clause 同款：精确段匹配 + 后跟非数字防 4.3 误中 4.30。

    纯数字章号（如「1」「7」）放宽为「后跟非数字」即可，允许命中「1. 总则」「7 附则」。
    """
    if not clause:
        return False
    if clause.isdigit():
        return bool(re.search(rf"(^|[\s/（(]){re.escape(clause)}(?![0-9])", text))
    return bool(re.search(rf"(^|[\s/（(]){re.escape(clause)}(?![0-9.])", text))


def _section_matches(section: str, clause: str) -> bool:
    return _clause_hits(section, clause)


async def main() -> int:
    async with async_session_factory() as db:
        kbs = (await db.execute(select(KnowledgeBase))).scalars().all()
        kb_by_name = {kb.name: kb.id for kb in kbs}

        rows = (
            await db.execute(
                select(Chunk.kb_id, Chunk.section, Chunk.clause_no, Chunk.content)
                .join(Document, Chunk.doc_id == Document.id)
                .where(Chunk.document_version_id == Document.active_version_id)
            )
        ).all()
        chunks_by_kb: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
        for kid, sec, clno, content in rows:
            chunks_by_kb[kid].append((sec or "", clno or "", content or ""))

        n_problem = 0
        for i, g in enumerate(GOLD, 1):
            kb_id = kb_by_name.get(g["kb"])
            if kb_id is None:
                print(f"[{i:2d}] ❌ 库不存在: {g['kb']}  ← {g['q']}")
                n_problem += 1
                continue
            chunks = chunks_by_kb.get(kb_id, [])
            all_content = "\n".join(c[2] for c in chunks)
            all_sections = "\n".join((c[0] + " " + c[1]) for c in chunks)

            problems: list[str] = []

            # 1. 条款号核对
            for cl in g.get("expect_clauses") or []:
                if not any(_section_matches(c[0] + " " + c[1], cl) for c in chunks):
                    problems.append(f"条款[{cl}]库内无对应section")

            # 2. 关键词核对（content + section + clause_no 都算）
            for kw in g.get("expect_keywords") or []:
                if kw not in all_content and kw not in all_sections:
                    problems.append(f"关键词[{kw}]库内找不到")

            # 3. note 章节号核对（提取 note 里的数字编号，查是否存在于库 section）
            note = g.get("note", "")
            note_nums = re.findall(r"\b\d+(?:\.\d+)*\b", note)
            for num in note_nums:
                # 排除纯年份（19xx/20xx）和页号/行号等无意义数字
                if re.fullmatch(r"(19|20)\d{2}", num):
                    continue
                # 只核对「纯条款号样式」的数字（含点，或单个 1-2 位章节号）
                if "." in num or (num.isdigit() and 1 <= len(num) <= 2):
                    if not any(_section_matches(c[0], num) for c in chunks):
                        problems.append(f"note章节[{num}]库内无对应section")

            if problems:
                n_problem += 1
                print(f"[{i:2d}] ⚠️ {g['q'][:34]}")
                print(f"      库={g['kb'][:28]}  intent={g['intent']}")
                print(f"      note={note[:50]}")
                print(f"      clauses={g.get('expect_clauses')}  keywords={g.get('expect_keywords')}")
                for p in problems:
                    print(f"        - {p}")
            else:
                print(f"[{i:2d}] ✅ {g['q'][:34]}")

        print(f"\n===== 共 {len(GOLD)} 题，{n_problem} 题有可疑信号 =====")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
