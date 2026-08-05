"""独立进程重建全部文档（清空 Chroma 后使用）。

场景：Chroma HNSW 索引损坏（并发 delete/write 竞态）后，需停服 → 清空 .chroma →
单进程重灌所有文档，避免再并发。扫描版 PDF 会重跑 OCR（约 5~6 分钟/份）。

用法（backend 目录，**必须先停止后端服务**）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/rebuild_all_standalone.py

说明：直接调用内部入库管线（_process_document），单进程串行，写 Chroma 无并发。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


async def main() -> None:
    import asyncio
    import time
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import Document
    from app.db.session import async_session_factory, init_db
    from app.modules.ingestion import manager as ingestion

    await init_db()
    settings.upload_dir_path.mkdir(parents=True, exist_ok=True)
    async with async_session_factory() as db:
        ids = list((await db.scalars(select(Document.id))).all())
    print(f"共 {len(ids)} 个文档，开始重建（扫描件 OCR 需数分钟）…")

    for did in ids:
        t0 = time.time()
        try:
            await ingestion._process_document(did)  # noqa: SLF001
            print(f"  doc={did} 完成，耗时 {time.time()-t0:.0f}s")
        except Exception:
            import logging

            logging.exception("doc=%s 重建失败", did)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
