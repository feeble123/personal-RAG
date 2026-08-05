"""独立进程入库真实 PDF（避免 uvicorn 与 OCR 并发共存导致的原生崩溃）。

用法（backend 目录）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/ingest_real_pdf.py "<pdf路径>" ["库名"]

说明：直接调用内部入库管线（解析 → OCR → 分块 → 向量化 → Chroma/BM25）。
"""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/ingest_real_pdf.py <pdf路径> [库名]")
        return
    pdf = Path(sys.argv[1])
    kb_name = sys.argv[2] if len(sys.argv) > 2 else "真实PDF测试库"
    if not pdf.exists():
        print(f"文件不存在: {pdf}")
        return

    import asyncio
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import Document, KnowledgeBase
    from app.db.session import async_session_factory, init_db
    from app.modules.ingestion import manager as ingestion

    async def run() -> None:
        await init_db()
        settings.upload_dir_path.mkdir(parents=True, exist_ok=True)
        async with async_session_factory() as db:
            kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
            if not kb:
                kb = KnowledgeBase(name=kb_name, description="真实 PDF 入库测试")
                db.add(kb)
                await db.commit()
                await db.refresh(kb)
                print(f"已创建知识库: {kb_name} (id={kb.id})")
            else:
                print(f"使用已有知识库: {kb_name} (id={kb.id})")

            # 同名文件去重
            dup = await db.scalar(select(Document).where(Document.kb_id == kb.id, Document.filename == pdf.name))
            if dup:
                print(f"已存在同名文档 doc_id={dup.id}，跳过（如需重解析请先删除）")
                return

            stored = f"{datetime.now(timezone.utc).strftime('%H%M%S%f')}_{pdf.name}"
            dest = settings.upload_dir_path / stored
            shutil.copy2(pdf, dest)
            doc = Document(
                kb_id=kb.id,
                filename=pdf.name,
                stored_path=stored,
                file_type="pdf",
                file_size=pdf.stat().st_size,
                status="pending",
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)
            print(f"开始入库 doc_id={doc.id}（扫描版 OCR 可能需要数分钟）…")

        t0 = time.time()
        await ingestion._process_document(doc.id)  # noqa: SLF001
        print(f"入库完成，耗时 {time.time()-t0:.0f}s")

        async with async_session_factory() as db:
            d = await db.get(Document, doc.id)
            print(f"文档: {d.filename} | 状态={d.status} | chunks={d.chunk_count} | 质量={d.quality}")
            kb = await db.get(KnowledgeBase, kb.id)
            print(f"知识库: {kb.name} | 文档={kb.doc_count} | chunks={kb.chunk_count}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
