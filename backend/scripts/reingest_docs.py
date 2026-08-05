"""定向重灌指定文档（修复标题识别后局部重建，不重跑未改动文档的 OCR）。

场景：标题识别/分块逻辑修复后，只需重新解析受影响的那几份文档，
其余文档的既有切片保持不变（Chroma 会在 _write_chunks 里整库重建，含全部文档）。

用法（backend 目录，**必须先停止后端服务**）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/reingest_docs.py 4 6
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


async def main() -> None:
    from sqlalchemy import select

    from app.db.models import Document
    from app.db.session import async_session_factory, init_db
    from app.modules.ingestion import manager as ingestion

    ids = [int(a) for a in sys.argv[1:]]
    if not ids:
        print("用法: python scripts/reingest_docs.py <doc_id> [...]")
        return
    await init_db()
    async with async_session_factory() as db:
        names = {
            d.id: d.filename
            for d in (await db.scalars(select(Document).where(Document.id.in_(ids)))).all()
        }
    for did in ids:
        if did not in names:
            print(f"  doc={did} 不存在，跳过")
            continue
        t0 = time.time()
        try:
            await ingestion._process_document(did)  # noqa: SLF001
            print(f"  doc={did} [{names[did]}] 完成，耗时 {time.time()-t0:.0f}s")
        except Exception:
            import logging

            logging.exception("doc=%s 重建失败", did)


if __name__ == "__main__":
    asyncio.run(main())
