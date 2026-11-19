"""把切片的坐标回填进 `chunks.bbox`（一次性维护脚本，**不用重灌**）。

背景：扫描件（如《水文预报》第 5 版）没有文本层，点「看原文」只能翻到那一页，
指不出是哪一段、哪张表。MinerU 解析时算过每个块的 bbox，存在
`data/mineru_output/<stem>/…/_middle.json` 里（PDF 点坐标），当初没落库。

做法：按页取块 → 用切片文本对齐（`services/bbox_match.py`）→ 写 `chunks.bbox`。
对不上的留 NULL，界面上如实显示"定位不到"，绝不硬指一个框。

用法（backend 目录下）：
    .venv\\Scripts\\python.exe -m scripts.backfill_chunk_bbox            # 只看覆盖率
    .venv\\Scripts\\python.exe -m scripts.backfill_chunk_bbox --apply    # 真写库
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

from app.services.bbox_match import (  # noqa: E402
    BlockRect,
    block_text,
    iter_lines,
    pick_block,
    unions,
)

MINERU_DIR = BASE_DIR / "data" / "mineru_output"


def _spans_text(block: dict) -> str:
    """块里的文字（表格块的表体在 middle.json 里是空的，只有表题有字）。"""
    parts = []
    for holder in iter_lines(block):
        if holder.get("type") == "table_body":
            continue
        for line in holder.get("lines") or []:
            for span in line.get("spans") or []:
                parts.append(str(span.get("content") or ""))
    return "".join(parts)


def _caption_text(block: dict) -> str:
    parts = []
    for sub in block.get("blocks") or []:
        if sub.get("type") == "table_caption":
            for line in sub.get("lines") or []:
                for span in line.get("spans") or []:
                    parts.append(str(span.get("content") or ""))
    return "".join(parts)


def load_pages(middle_path: Path) -> dict[int, list[BlockRect]]:
    """{page_idx: [候选块]}，坐标是 PDF 点。"""
    data = json.loads(middle_path.read_text(encoding="utf-8"))
    pages: dict[int, list[BlockRect]] = {}
    for page in data.get("pdf_info") or []:
        idx = page.get("page_idx")
        if idx is None:
            continue
        blocks: list[BlockRect] = []
        for block in page.get("preproc_blocks") or []:
            kind = str(block.get("type") or "")
            bbox = block.get("bbox")
            if kind not in {"text", "title", "table", "list", "figure"} or not bbox:
                continue
            text = block_text(kind, _spans_text(block), _caption_text(block))
            rect = tuple(float(v) for v in bbox[:4])
            blocks.append(BlockRect(kind=kind, rect=rect, text=text))  # type: ignore[arg-type]
        pages[idx] = blocks
    return pages


def find_middle_json(stem: str) -> Path | None:
    """按 uploaded 文件名（去掉扩展名）找 MinerU 目录。"""
    for candidate in MINERU_DIR.glob(f"{stem}/*/*_middle.json"):
        if candidate.is_file():
            return candidate
    # 有些产物多套一层目录
    for candidate in MINERU_DIR.glob(f"{stem}/**/*_middle.json"):
        if candidate.is_file():
            return candidate
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description="回填切片在原 PDF 上的坐标")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    parser.add_argument("--doc-id", type=int, default=None, help="只处理某个文档")
    args = parser.parse_args()

    from sqlalchemy import select

    from app.db.models import Chunk, Document
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        docs = (await db.scalars(select(Document).where(Document.file_type == "pdf"))).all()
        for doc in docs:
            if args.doc_id is not None and doc.id != args.doc_id:
                continue
            stem = Path(doc.stored_path).stem
            middle = find_middle_json(stem)
            if middle is None:
                print(f"doc{doc.id:<3} {doc.filename[:36]:<36} 没有 MinerU 产物，跳过")
                continue

            pages = load_pages(middle)
            chunks = (
                await db.scalars(select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_index))
            ).all()
            hit = miss = noref = 0
            for chunk in chunks:
                if not chunk.page:
                    noref += 1
                    continue
                blocks = pages.get(chunk.page - 1) or []
                block = pick_block(
                    blocks,
                    chunk.content or "",
                    chunk_is_table=(chunk.block_type or "") == "table",
                )
                if block is None:
                    miss += 1
                    continue
                hit += 1
                if args.apply:
                    chunk.bbox = {
                        "rects": unions([block.rect]),
                        "source": "mineru",
                        "page_idx": chunk.page - 1,
                        "block_kind": block.kind,
                    }
            if args.apply:
                await db.commit()
            total = hit + miss
            ratio = (hit / total * 100) if total else 0.0
            print(
                f"doc{doc.id:<3} {doc.filename[:36]:<36} 命中 {hit}/{total} ({ratio:.0f}%)"
                f"  无页码 {noref}" + ("  ← 已写入" if args.apply else "")
            )
        if not args.apply:
            print("\n（未改动，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
