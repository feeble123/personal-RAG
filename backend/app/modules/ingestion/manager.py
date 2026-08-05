"""后台入库任务：解析 → 分块 → 向量化（缓存）→ Chroma → BM25 更新。

- 信号量限制并发入库（≤2），避免 SQLite 写锁竞争
- 解析/OCR/Chroma 写盘为 CPU 密集 → 走 to_thread 不阻塞事件循环
- 失败自动回写 documents.status=failed + error_message
- 重解析：先删旧 chunk/向量，再入库（幂等）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import async_session_factory
from app.services import bm25, semantic_cache, vector_store
from app.services.chunker import chunk_blocks
from app.services.embedding import embed_documents, load_cache_vectors, store_cache_vectors
from app.services.parser.factory import get_parser
from app.services.parser.ocr_progress import clear_progress

logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(2)
_running: dict[int, asyncio.Task] = {}
# 内容去重 + 写入串行锁：并发入库时两个任务可能同时查 content_hash → 双双错过未提交的
# 插入 → UNIQUE 冲突（实测 doc3 因与其他扫描件产生相同短块哈希而入库失败）。加锁保证
# 「查重 + 写入」原子，同时避免 embedding 批处理与 Chroma 写盘的并发竞争。
_write_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue_ingestion(doc_id: int) -> None:
    """投递入库任务（幂等：已在处理则忽略）。"""
    existing = _running.get(doc_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_run_ingestion(doc_id))
    _running[doc_id] = task
    task.add_done_callback(lambda t: _running.pop(doc_id, None))


async def _run_ingestion(doc_id: int) -> None:
    try:
        async with _semaphore:
            await _process_document(doc_id)
    except Exception as exc:
        logger.exception("入库失败 doc=%s", doc_id)
        try:
            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                if doc:
                    doc.status = "failed"
                    doc.error_message = str(exc)[:1000]
                    clear_progress(doc.stored_path)
                    await db.commit()
        except Exception:
            logger.exception("回写失败状态失败 doc=%s", doc_id)


async def _process_document(doc_id: int) -> None:
    async with async_session_factory() as db:
        doc = await db.get(Document, doc_id)
        if doc is None:
            return
        kb = await db.get(KnowledgeBase, doc.kb_id)
        if kb is None:
            return

        clear_progress(doc.stored_path)  # 清除残留进度，开始全新一轮
        doc.status = "parsing"
        doc.error_message = None
        kb.status = "indexing"
        await db.commit()

        path = settings.upload_dir_path / doc.stored_path
        parser = get_parser(doc.filename)
        parsed = await asyncio.to_thread(parser.parse, path, doc.filename)

        doc.status = "embedding"
        doc.page_count = parsed.page_count
        doc.quality = parsed.quality
        await db.commit()

        chunks = chunk_blocks(parsed.blocks)
        await _write_chunks(db, doc, kb, chunks)

        # 完成
        doc.status = "ready"
        doc.parsed_at = _now()
        doc.chunk_count = len(chunks)
        await _refresh_kb_stats(db, doc.kb_id)
        await db.commit()
        logger.info("文档入库完成 doc=%s chunks=%d", doc_id, len(chunks))
    # 在会话外重建 BM25（只读）；清空语义缓存（新文档入库后旧答案失效）
    await _rebuild_bm25(doc.kb_id)
    await semantic_cache.clear_cache()


async def _write_chunks(db, doc: Document, kb: KnowledgeBase, chunks: list[Any]) -> None:
    """写入 DB + 整库重建 Chroma + embedding 缓存。

    不用 `delete_by_where + add_vectors` 更新 Chroma：反复 delete+add 会损坏 HNSW 索引
    （实测 "Error loading hnsw index"，整个集合查询崩溃）。改为 DB 入库后整库重建，
    保证 Chroma 与 DB 始终一致且索引健康。
    """
    async with _write_lock:
        # 1) 删除该文档旧切片（DB）
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc.id))
        await db.commit()

        # 2) 去重：库内已有哈希 + 本批内重复（同 content_hash 只保留首个）。
        # 实测 OCR 噪声切片（如标题+页码「## 前言\n6」）可能同 doc 出现两次，
        # 若只查库不去重本批，flush 时同批两条相同哈希 → UNIQUE 冲突 → 整库回滚。
        hashes = [c.content_hash for c in chunks]
        existing_hashes = set(
            (await db.scalars(select(Chunk.content_hash).where(Chunk.content_hash.in_(hashes)))).all()
        )
        new_chunks: list[Any] = []
        seen: set[str] = set(existing_hashes)
        for c in chunks:
            if c.content_hash in seen:
                continue
            seen.add(c.content_hash)
            new_chunks.append(c)
        if not new_chunks:
            return

        # 3) 向量化（DB 缓存命中跳过 API 调用）
        cache = await load_cache_vectors(db, [c.content_hash for c in new_chunks])
        missing = [c for c in new_chunks if c.content_hash not in cache]
        vectors = await embed_documents([c.content for c in missing])
        vec_map: dict[str, list[float]] = {}
        it = iter(vectors)
        for c in new_chunks:
            if c.content_hash in cache:
                vec_map[c.content_hash] = cache[c.content_hash]
            else:
                vec_map[c.content_hash] = next(it)
        if vec_map:
            await store_cache_vectors(db, vec_map)

        # 4) DB 入库（拿自增 chunk id）
        for i, c in enumerate(new_chunks):
            db.add(
                Chunk(
                    kb_id=doc.kb_id,
                    doc_id=doc.id,
                    chunk_index=i,
                    content=c.content,
                    section=c.section,
                    page=c.page,
                    content_hash=c.content_hash,
                )
            )
        await db.flush()

    # 5) 整库重建 Chroma（读全部 DB 切片 → 重置 collection → 全量写入）
    await _rebuild_chroma(db)


async def _rebuild_chroma(db) -> None:
    """从 DB 全部切片重建 Chroma（缓存命中不调 API，重置后全量写入）。"""
    rows = (
        await db.execute(
            select(Chunk, Document).join(Document, Chunk.doc_id == Document.id).order_by(Chunk.id)
        )
    ).all()
    if not rows:
        await asyncio.to_thread(vector_store.reset_collection)
        return
    hashes = [c.content_hash for c, _ in rows]
    cache = await load_cache_vectors(db, hashes)
    vec_map: dict[str, list[float]] = {}
    for c, _ in rows:
        v = cache.get(c.content_hash)
        if v is not None:
            vec_map[c.content_hash] = v
    missing = [c for c, _ in rows if c.content_hash not in vec_map]
    if missing:
        vectors = await embed_documents([c.content for c in missing])
        for c, v in zip(missing, vectors):
            vec_map[c.content_hash] = v
    await asyncio.to_thread(vector_store.reset_collection)
    await asyncio.to_thread(
        vector_store.add_vectors,
        ids=[str(c.id) for c, _ in rows],
        embeddings=[vec_map[c.content_hash] for c, _ in rows],
        documents=[c.content for c, _ in rows],
        metadatas=[
            {
                "kb_id": d.kb_id,
                "doc_id": d.id,
                "chunk_id": c.id,
                "source": d.filename,
                "page": c.page or 0,
                "section": c.section or "",
            }
            for c, d in rows
        ],
    )


async def _refresh_kb_stats(db, kb_id: int) -> None:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        return
    kb.doc_count = (
        await db.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb_id))
    ) or 0
    kb.chunk_count = (
        await db.scalar(select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id))
    ) or 0
    kb.status = "ready" if kb.chunk_count else ("indexing" if kb.doc_count else "empty")


async def _rebuild_bm25(kb_id: int) -> None:
    try:
        async with async_session_factory() as db:
            rows = await db.execute(select(Chunk.id, Chunk.content).where(Chunk.kb_id == kb_id))
            items = [(r[0], r[1]) for r in rows]
        await asyncio.to_thread(bm25.rebuild, kb_id, items)
    except Exception:
        logger.exception("BM25 重建失败 kb=%s", kb_id)


# ---------- 删除清理（供路由调用） ----------
async def delete_document(doc_id: int) -> None:
    """删除文档：Chroma 向量 + chunks + 文件 + 文档记录，刷新统计与 BM25。

    注意：全程 bulk delete（DB 级 FK CASCADE 处理 citations），
    不把子对象加载进 ORM 会话，避免与级联删除发生 StaleDataError。
    """
    from app.db.models import Document as Doc

    async with async_session_factory() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return
        kb_id = doc.kb_id
        stored_path = doc.stored_path
        await asyncio.to_thread(vector_store.delete_by_where, {"doc_id": doc_id})
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        # 物理文件删除（忽略失败）
        try:
            (settings.upload_dir_path / stored_path).unlink(missing_ok=True)
        except Exception:
            pass
        await db.execute(delete(Doc).where(Doc.id == doc_id))
        await _refresh_kb_stats(db, kb_id)
        await db.commit()
    await _rebuild_bm25(kb_id)
    await semantic_cache.clear_cache()


async def delete_kb(kb_id: int) -> None:
    """删除知识库：Chroma 全量 + chunks + 文档 + 文件 + 库记录。

    注意：文件路径用轻量 select 获取（不加载 ORM 子对象），
    删除全部走 bulk delete（DB 级 CASCADE），最后对象删除 KB 本身。
    """
    from app.db.models import Document as Doc
    from app.db.models import KnowledgeBase as KB

    async with async_session_factory() as db:
        paths = (await db.execute(select(Doc.stored_path).where(Doc.kb_id == kb_id))).scalars().all()
        for p in paths:
            try:
                (settings.upload_dir_path / p).unlink(missing_ok=True)
            except Exception:
                pass
        await asyncio.to_thread(vector_store.delete_by_where, {"kb_id": kb_id})
        await db.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
        await db.execute(delete(Doc).where(Doc.kb_id == kb_id))
        kb = await db.get(KB, kb_id)
        if kb is not None:
            await db.delete(kb)
        await db.commit()
    await asyncio.to_thread(bm25.remove_kb, kb_id)
    await semantic_cache.clear_cache()
