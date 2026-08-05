"""从 DB 切片直接重建 Chroma 向量库（不重新解析，避免 delete+add 损坏 HNSW）。

场景：Chroma HNSW 索引损坏（delete_by_where 反复 delete+add 会把索引写坏），
但 DB 中的切片内容仍是正确的。停后端后运行，重建向量库即可。

用法（backend 目录，**必须先停止后端服务**）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/rebuild_chroma_from_db.py

说明：读取所有 chunks，嵌入（DB 缓存命中跳过 API），写入全新 collection。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


async def main() -> None:
    import asyncio
    import logging
    import time
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import Chunk, Document
    from app.db.session import async_session_factory, init_db
    from app.services import vector_store
    from app.services.embedding import embed_documents, load_cache_vectors

    await init_db()
    settings.upload_dir_path.mkdir(parents=True, exist_ok=True)

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Chunk, Document)
                .join(Document, Chunk.doc_id == Document.id)
                .order_by(Chunk.id)
            )
        ).all()
        hashes = [c.content_hash for c, _ in rows]
        cache = await load_cache_vectors(db, hashes)

    if not rows:
        print("DB 无切片，跳过")
        return

    vec_map: dict[str, list[float]] = {}
    for c, _ in rows:
        v = cache.get(c.content_hash)
        if v is not None:
            vec_map[c.content_hash] = v
    missing = [c for c, _ in rows if c.content_hash not in vec_map]
    if missing:
        t0 = time.time()
        vectors = await embed_documents([c.content for c in missing])
        for c, v in zip(missing, vectors):
            vec_map[c.content_hash] = v
        print(f"新嵌入 {len(missing)} 个（{time.time()-t0:.0f}s）")
    else:
        print("全部命中 embedding 缓存，无 API 调用")

    ids = [str(c.id) for c, _ in rows]
    documents = [c.content for c, _ in rows]
    embeddings = [vec_map[c.content_hash] for c, _ in rows]
    metadatas = [
        {
            "kb_id": d.kb_id,
            "doc_id": d.id,
            "chunk_id": c.id,
            "source": d.filename,
            "page": c.page or 0,
            "section": c.section or "",
        }
        for c, d in rows
    ]
    try:
        vector_store.add_vectors(ids, embeddings, documents, metadatas)
        print(f"已写入 {len(ids)} 个向量到 Chroma")
    except Exception as exc:  # noqa: BLE001
        logging.exception("写入 Chroma 失败")
        print(f"失败: {str(exc)[:200]}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
