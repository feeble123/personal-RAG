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
    from datetime import datetime, timezone
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import Chunk, Document
    from app.db.session import async_session_factory, ensure_db_at_head
    from app.services import vector_store
    from app.services.embedding import embed_documents, load_cache_vectors

    await ensure_db_at_head()
    settings.upload_dir_path.mkdir(parents=True, exist_ok=True)

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Chunk, Document)
                .join(Document, Chunk.doc_id == Document.id)
                .where(Chunk.document_version_id == Document.active_version_id)
                .order_by(Chunk.id)
            )
        ).all()

    if not rows:
        print("DB 无 active 切片，跳过")
        return

    # 按知识库分组
    by_kb: dict[int, list[tuple]] = {}
    for c, d in rows:
        by_kb.setdefault(d.kb_id, []).append((c, d))

    hashes = [c.content_hash for c, _ in rows]
    cache = await load_cache_vectors(db, hashes)

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

    # 每个库重建一个版本号 collection，并更新 IndexVersion 指针（软切换）
    from app.db.models import IndexVersion, KnowledgeBase

    async with async_session_factory() as db:
        for kb_id, items in by_kb.items():
            existing = (
                await db.execute(select(IndexVersion.id).where(IndexVersion.kb_id == kb_id))
            ).scalars().all()
            next_v = (max(existing) if existing else 0) + 1
            name = f"{settings.chroma_collection}_kb{kb_id}_v{next_v}"

            ids = [str(c.id) for c, _ in items]
            documents = [c.content for c, _ in items]
            embeddings = [vec_map[c.content_hash] for c, _ in items]
            metadatas = [
                {
                    "kb_id": d.kb_id,
                    "doc_id": d.id,
                    "chunk_id": c.id,
                    "source": d.filename,
                    "page": c.page or 0,
                    "section": c.section or "",
                }
                for c, d in items
            ]
            try:
                actual = await asyncio.to_thread(
                    vector_store.build_collection, name, ids, embeddings, documents, metadatas
                )
                if actual != len(items):
                    raise RuntimeError(f"count 核对失败: expected={len(items)} actual={actual}")
                iv = IndexVersion(
                    kb_id=kb_id,
                    status="active",
                    physical_name=name,
                    expected_count=len(items),
                    actual_count=len(items),
                    activated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                db.add(iv)
                await db.flush()
                kb = await db.get(KnowledgeBase, kb_id)
                if kb is not None:
                    prev_iv_id = kb.active_index_version_id
                    kb.active_index_version_id = iv.id
                    if prev_iv_id is not None and prev_iv_id != iv.id:
                        old_iv = await db.get(IndexVersion, prev_iv_id)
                        if old_iv is not None:
                            old_iv.status = "retired"
                            old_iv.retired_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await db.commit()
                print(f"kb={kb_id} 已重建 {len(ids)} 个向量 → {name}")
            except Exception as exc:  # noqa: BLE001
                logging.exception("重建 kb=%s 失败", kb_id)
                print(f"kb={kb_id} 失败: {str(exc)[:200]}")
                await db.rollback()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
