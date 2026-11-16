"""回填切片的「书内印刷页码」（一次性维护脚本）。

**为什么需要**：引用里显示的一直是 PDF 物理页序。用户拿着书翻，书上印的是 157，
PDF 却是第 171 页——差的 14 页是封面/版权/前言/目录。能点开原文却翻不到那一页。

MinerU 其实逐页认出了印刷页码（`page_number` 元素），当初当成页眉噪音丢掉了。
现在解析时会留住它；这个脚本负责**把已经有 MinerU 产物的历史文档补上**，
不用重新解析（那些 content_list.json 都还在 data/mineru_output 下）。

用法（backend 目录下）：
    .venv\\Scripts\\python.exe -m scripts.backfill_printed_page            # 只看
    .venv\\Scripts\\python.exe -m scripts.backfill_printed_page --apply    # 真改
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _content_list_for(stored_path: str) -> Path | None:
    """按 stored_path 找 MinerU 的 content_list.json（目录结构是 <stem>/<stem>/ocr/）。"""
    stem = Path(stored_path or "").stem
    if not stem:
        return None
    from app.core.config import settings

    root = settings.data_dir / "mineru_output" / stem
    if not root.exists():
        return None
    for candidate in sorted(root.rglob("*_content_list.json")):
        if candidate.name.endswith("_content_list_v2.json"):
            continue  # v2 是另一套结构，不在这里处理
        return candidate
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description="回填书内印刷页码")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    parser.add_argument("--doc-id", type=int, default=None, help="只处理某个文档")
    args = parser.parse_args()

    from sqlalchemy import func, select, update

    from app.db.models import Chunk, Document
    from app.db.session import async_session_factory
    from app.services.parser.mineru import build_page_number_map

    async with async_session_factory() as db:
        stmt = select(Document)
        if args.doc_id:
            stmt = stmt.where(Document.id == args.doc_id)
        docs = list((await db.scalars(stmt)).all())

        total_chunks = 0
        touched_docs = 0
        for doc in docs:
            path = _content_list_for(doc.stored_path)
            if path is None:
                continue
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - 坏产物跳过并说清楚
                print(f"  跳过 {doc.filename}：产物读不出来（{exc}）")
                continue
            mapping = build_page_number_map(content)
            if not mapping:
                print(f"  跳过 {doc.filename}：没识别出可信的页码（就不猜）")
                continue

            pages = list(mapping)
            pending = await db.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(
                    Chunk.doc_id == doc.id,
                    Chunk.printed_page.is_(None),
                    Chunk.page.in_(pages),
                )
            )
            pending = int(pending or 0)
            print(
                f"  {doc.filename}: 认出 {len(mapping)} 页页码"
                f"（例：物理 {pages[len(pages) // 2]} → 印刷 {mapping[pages[len(pages) // 2]]}），"
                f"可补 {pending} 个切片"
            )
            total_chunks += pending
            if args.apply and pending:
                for page, printed in mapping.items():
                    await db.execute(
                        update(Chunk)
                        .where(
                            Chunk.doc_id == doc.id,
                            Chunk.printed_page.is_(None),
                            Chunk.page == page,
                        )
                        .values(printed_page=printed)
                    )
                touched_docs += 1

        if args.apply and total_chunks:
            await db.commit()

        # 第二步：实例证据里存的是当时的页码（物理页），把印刷页补进去，
        # 界面上「依据」那一行才不会还是 171。
        evidence_rows = await _backfill_evidence(db, apply=args.apply)

        print("-" * 60)
        print(
            f"共可补 {total_chunks} 个切片"
            + (f"（已写入 {touched_docs} 个文档）" if args.apply and total_chunks else "（未改动，加 --apply 执行）")
        )
        print(
            f"共可补 {evidence_rows} 条实例证据"
            + ("（已写入）" if args.apply and evidence_rows else "（未改动）")
        )
        if args.apply and evidence_rows and not total_chunks:
            await db.commit()
    return 0


async def _backfill_evidence(db, *, apply: bool) -> int:
    """给实例证据补 printed_page（按它已经锚定的 chunk_id 查）。"""
    from sqlalchemy import select

    from app.db.models import Chunk, OntologyInstance

    rows = list((await db.scalars(select(OntologyInstance))).all())
    pending: list[tuple[OntologyInstance, list]] = []
    for row in rows:
        items = list(row.evidence or [])
        if not items:
            continue
        changed = False
        new_items = []
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            chunk_id = item.get("chunk_id")
            if chunk_id is None or item.get("printed_page") is not None:
                new_items.append(item)
                continue
            chunk = await db.get(Chunk, int(chunk_id))
            printed = getattr(chunk, "printed_page", None) if chunk is not None else None
            if printed is None:
                new_items.append(item)
                continue
            new_items.append({**item, "printed_page": printed})
            changed = True
        if changed:
            pending.append((row, new_items))
    for row, items in pending:
        if apply:
            row.evidence = items
    if apply and pending:
        await db.commit()
    return len(pending)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
