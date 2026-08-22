"""后台入库任务：解析 → 分块 → 向量化（缓存）→ 版本发布 → Chroma → BM25 更新。

P0-8 版本化入库：
- 每次重灌创建 target DocumentVersion（building），**不再先删旧 chunk**
- 失败 → target=failed，旧 active 版本 + 旧 chunk 原样保留（可查、可回滚）
- 成功 → 原子发布：doc.active_version_id 指向 target、target=active、旧版=retired
- Chroma/BM25 只含 active 版本（旧 retired 保留在 DB 但不进索引，回滚 = pointer 指回）

信号量限制并发入库（≤2），避免 SQLite 写锁竞争；解析/OCR/Chroma 写盘走 to_thread。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db.models import Chunk, Document, DocumentVersion, KnowledgeBase
from app.db.session import async_session_factory
from app.services import bm25, semantic_cache, vector_store
from app.services.chunker import chunk_blocks, chunk_toc_pages
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


def _source_hash(path) -> str | None:
    """源文件 sha256（探测同一文件重灌是否真的变了；读盘失败返回 None 不阻塞）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


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
                    # P0-8：失败只标 target 版本 failed，旧 active 版本不受影响
                    target = await db.scalar(
                        select(DocumentVersion).where(
                            DocumentVersion.document_id == doc_id,
                            DocumentVersion.status == "building",
                        )
                    )
                    if target:
                        target.status = "failed"
                        target.error_message = str(exc)[:1000]
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
        # P0-8：创建 target 版本（building）。旧 active 版本不删，失败则旧版可用。
        target = DocumentVersion(
            document_id=doc.id,
            status="building",
            source_hash=_source_hash(settings.upload_dir_path / doc.stored_path),
            parser_profile={
                "parser": get_parser(doc.filename).__class__.__name__,
                "chunk_strategy": doc.chunk_strategy or settings.chunk_strategy_default,
            },
            chunk_profile={
                "chunk_size": settings.chunk_size,
                "chunk_overlap": settings.chunk_overlap,
            },
        )
        db.add(target)
        await db.commit()
        await db.refresh(target)
        # P0-3 失效安全：重灌一开始就清语义缓存——若中途失败（旧切片已删而缓存未清），
        # 也不会重放引用已删切片的旧答案（此前只在成功路径 line199 清理）
        await semantic_cache.clear_cache()

        path = settings.upload_dir_path / doc.stored_path
        parser = get_parser(doc.filename)
        # 切片策略（上传时选择）：old=经典启发式 / new=目录+LLM断号补全
        chunk_strategy = doc.chunk_strategy or settings.chunk_strategy_default
        parsed = await asyncio.to_thread(parser.parse, path, doc.filename, chunk_strategy)

        # 断号自检（OCR 偶发漏行防护）：扫描条款号缺失，命中则用更高条带数重 OCR
        # 受影响页补回（自校验：补不回视为规范本身跳号，不改动）
        gap_info = None
        if hasattr(parser, "repair_ocr_gaps"):
            from app.services.parser.clause_gap import check_clause_gaps

            gaps = check_clause_gaps(parsed.blocks)
            if gaps:
                logger.info("检测到条款断号 doc=%s 节=%s", doc_id, [g["section"] for g in gaps])
                repaired = await asyncio.to_thread(parser.repair_ocr_gaps, path, parsed, gaps)
                if repaired is not None:
                    parsed.blocks = repaired
                    gap_info = {
                        "detected": len(gaps),
                        "sections": [g["section"] for g in gaps],
                        "repaired": True,
                    }
                else:
                    gap_info = {
                        "detected": len(gaps),
                        "sections": [g["section"] for g in gaps],
                        "repaired": False,
                    }

        # 水印/广告噪声过滤（「只增不减」的例外：广告不是知识，是检索噪声）——
        # 跨页重复行（每页水印）确定性移除 + LLM 兜底判单页偶发广告；fake/异常降级纯规则不阻塞。
        from app.services.parser import boilerplate as bp

        boilerplate_removed: list[str] = []
        try:
            parsed.blocks, repeated, boilerplate_removed = bp.filter_repeated_lines(
                parsed.blocks, parsed.page_count
            )
            if parsed.toc_texts:
                parsed.toc_texts = {
                    p: bp.filter_text_lines(t, repeated) for p, t in parsed.toc_texts.items()
                }
            candidates = bp.collect_ad_candidates(parsed.blocks)
            if candidates:
                llm_remove = await bp.llm_filter_ads(candidates)
                if llm_remove:
                    parsed.blocks = bp.remove_lines(parsed.blocks, set(llm_remove))
                    boilerplate_removed.extend(llm_remove)
        except Exception:
            logger.exception("水印/广告过滤失败，跳过（不阻塞入库）")
        parsed.quality["boilerplate_removed_lines"] = len(set(boilerplate_removed))

        # 目录权威大纲 + LLM 断号补全（切片保险）：TOC 补 1/2 级硬边界，
        # LLM 确认缺失的 3/4/5 级注入软边界。必须在 repair_ocr_gaps 之后（重 OCR 按页重建
        # blocks 会冲掉注入块）、chunk_blocks 之前。LLM 失败降级为全候选，不阻塞入库。
        if chunk_strategy == "new" and settings.gap_check_enabled:
            from app.services.parser import gap_check
            from app.services.parser import outline as outline_mod

            try:
                found = gap_check.scan_numbered_lines(parsed.blocks)
                confirmed = await gap_check.confirm_missing(parsed.outline, found)
                llm_used = confirmed is not None
                if confirmed is None:
                    confirmed = set(gap_check.candidate_missing(parsed.outline, found))
                parsed.blocks = outline_mod.inject_blocks(parsed.blocks, parsed.outline, confirmed, found)
                parsed.quality["outline"] = {
                    "toc_entries": len(parsed.outline.entries) if parsed.outline else 0,
                    "toc_offset": parsed.outline.offset if parsed.outline else None,
                    "toc_pages": len(parsed.outline.toc_pages) if parsed.outline else 0,
                    "confirmed_missing": len(confirmed),
                    "llm_used": llm_used,
                }
                parsed.quality["chunk_strategy"] = "new"
            except Exception:
                # 增强是「保险」：任何异常都回退经典切片结果（parsed.blocks 保持原样），不阻塞入库
                logger.exception("目录+LLM补全切片增强失败，降级为经典切片（不阻塞入库）")

        doc.status = "embedding"
        doc.page_count = parsed.page_count
        doc.quality = parsed.quality
        if gap_info:
            doc.quality = {**parsed.quality, "gap_check": gap_info}
        await db.commit()

        chunks = chunk_blocks(parsed.blocks)
        # 目录内容单独成「目录」切片（只增不减：原文件所有内容都必须进知识库）
        if getattr(parsed, "toc_texts", None):
            toc_chunks = chunk_toc_pages(parsed.toc_texts)
            if toc_chunks:
                chunks = toc_chunks + chunks
        # 完整性自检：原文件每行是否都保留在切片中（不阻塞入库，作预警 + 答辩数据）
        from app.services.parser.completeness import check_content_completeness

        comp = check_content_completeness(parsed.blocks, chunks)
        # 整体重赋而非原地改 dict：SQLAlchemy JSON 列原地突变可能不标记 dirty → 不落库
        doc.quality = {**doc.quality, "content_completeness": comp}
        if not comp["complete"]:
            logger.warning(
                "切片内容完整性缺失 doc=%s missing_lines=%d pages=%s",
                doc_id,
                comp["missing_lines"],
                comp["missing_pages"],
            )
        target.quality_json = doc.quality
        await _write_chunks(db, doc, target, chunks)

        # P0-8 影子索引：构建「新世界」快照（所有文档 active + 本 target）→ 核对 count →
        # 通过后再原子发布 pointer。任何失败走 _run_ingestion 失败路径，旧 active 不动。
        await _build_shadow_index(db, doc, target)
        # 原子发布：切 active pointer + target=active + 旧版=retired
        await _publish_version(db, doc, target)
        logger.info("文档入库完成 doc=%s chunks=%d version=%s", doc_id, len(chunks), target.id)
    # 在会话外重建 BM25（只读）；清空语义缓存（新文档入库后旧答案失效）
    await _rebuild_bm25(doc.kb_id)
    await semantic_cache.clear_cache()


async def _write_chunks(
    db, doc: Document, target: DocumentVersion, chunks: list[Any]
) -> None:
    """写入 target 版本的 DB chunks + embedding 缓存。

    P0-8：**不再删旧 chunks**——新 chunks 全挂到 target 版本；发布成功前旧 active
    版本原样保留（故障时旧版可查）。embedding 缓存按 content_hash 复用（P0-7）。
    """
    async with _write_lock:
        # 向量化（DB 缓存命中跳过 API 调用）。
        hash_to_content: dict[str, str] = {}
        for c in chunks:
            hash_to_content.setdefault(c.content_hash, c.content)
        cache = await load_cache_vectors(db, list(hash_to_content))
        missing_hashes = [h for h in hash_to_content if h not in cache]
        vec_map: dict[str, list[float]] = dict(cache)
        if missing_hashes:
            vectors = await embed_documents([hash_to_content[h] for h in missing_hashes])
            for h, v in zip(missing_hashes, vectors):
                vec_map[h] = v
            await store_cache_vectors(db, vec_map)

        # DB 入库（拿自增 chunk id）——挂 target 版本，chunk_index = 原始切片序号
        for i, c in enumerate(chunks):
            db.add(
                Chunk(
                    kb_id=doc.kb_id,
                    doc_id=doc.id,
                    document_version_id=target.id,
                    chunk_index=i,
                    content=c.content,
                    section=c.section,
                    page=c.page,
                    content_hash=c.content_hash,
                )
            )
        target.chunk_count = len(chunks)
        await db.flush()


async def _publish_version(db, doc: Document, target: DocumentVersion) -> None:
    """原子发布：单事务切 active pointer + target=active + 旧版=retired + 文档 ready。

    旧 active 版本的 chunks **保留在 DB**（回滚 = pointer 指回前一版即可）；检索层
    只查 active（见 rag.py），retired 不进 Chroma/BM25 索引。
    """
    prev = await db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id,
            DocumentVersion.status == "active",
        )
    )
    now = _now()
    if prev is not None:
        prev.status = "retired"
        prev.retired_at = now
    target.status = "active"
    target.activated_at = now
    doc.active_version_id = target.id
    doc.status = "ready"
    doc.parsed_at = now
    doc.chunk_count = target.chunk_count
    await _refresh_kb_stats(db, doc.kb_id)
    await db.commit()


def _active_chunk_rows(rows) -> list:
    """从 (Chunk, Document) 行中筛出 active 版本的行（索引/统计只含 active）。"""
    active_ids = {d.active_version_id for _, d in rows if d.active_version_id is not None}
    return [(c, d) for c, d in rows if c.document_version_id in active_ids]


async def _build_shadow_index(db, doc: Document, target: DocumentVersion) -> None:
    """构建「新世界」影子索引：所有文档 active 版本 + 本 target 的切片。

    P0-8 核心：建 shadow collection（不碰 active），核对 count 一致后原子改名切换。
    - 失败 → drop_shadow + 抛异常 → _run_ingestion 标 target=failed，旧 active collection 原样可查
    - 成功 → shadow 变 active，新内容立即可查（发布 pointer 在其后单事务进行）
    """
    rows = (
        await db.execute(
            select(Chunk, Document).join(Document, Chunk.doc_id == Document.id).order_by(Chunk.id)
        )
    ).all()
    # 「新世界」active 集合：其他文档按 active_version_id，本文档用 target
    active_ids = {d.active_version_id for _, d in rows if d.active_version_id is not None}
    active_ids.discard(doc.active_version_id)  # 本文档旧版本让位给 target
    active_ids.add(target.id)
    sel_rows = [(c, d) for c, d in rows if c.document_version_id in active_ids]

    if not sel_rows:
        await asyncio.to_thread(vector_store.drop_shadow)
        await asyncio.to_thread(vector_store.reset_collection)
        return
    hashes = [c.content_hash for c, _ in sel_rows]
    cache = await load_cache_vectors(db, hashes)
    vec_map: dict[str, list[float]] = {}
    for c, _ in sel_rows:
        v = cache.get(c.content_hash)
        if v is not None:
            vec_map[c.content_hash] = v
    missing = [c for c, _ in sel_rows if c.content_hash not in vec_map]
    if missing:
        vectors = await embed_documents([c.content for c in missing])
        for c, v in zip(missing, vectors):
            vec_map[c.content_hash] = v
    ids = [str(c.id) for c, _ in sel_rows]
    embeddings = [vec_map[c.content_hash] for c, _ in sel_rows]
    documents = [c.content for c, _ in sel_rows]
    metadatas = [
        {
            "kb_id": d.kb_id,
            "doc_id": d.id,
            "chunk_id": c.id,
            "source": d.filename,
            "page": c.page or 0,
            "section": c.section or "",
        }
        for c, d in sel_rows
    ]
    try:
        actual = await asyncio.to_thread(
            vector_store.build_shadow, ids, embeddings, documents, metadatas
        )
        if actual != len(sel_rows):
            raise RuntimeError(
                f"影子索引 count 核对失败: expected={len(sel_rows)} actual={actual}"
            )
        await asyncio.to_thread(vector_store.swap_shadow_to_active)
    except Exception:
        # 失败：清 shadow（active 原样保留），上报让 target 标 failed
        try:
            await asyncio.to_thread(vector_store.drop_shadow)
        except Exception:
            logger.exception("drop_shadow 失败（忽略）")
        raise


async def _refresh_kb_stats(db, kb_id: int) -> None:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        return
    kb.doc_count = (
        await db.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb_id))
    ) or 0
    # P0-8：chunk_count 只统计 active 版本（retired 不可查不计）
    kb.chunk_count = (
        await db.scalar(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.kb_id == kb_id, Chunk.document_version_id == Document.active_version_id)
        )
    ) or 0
    kb.status = "ready" if kb.chunk_count else ("indexing" if kb.doc_count else "empty")


async def _rebuild_bm25(kb_id: int) -> None:
    try:
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(Chunk.id, Chunk.content)
                    .join(Document, Chunk.doc_id == Document.id)
                    .where(
                        Chunk.kb_id == kb_id,
                        Chunk.document_version_id == Document.active_version_id,
                    )
                )
            ).all()
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
    from app.db.models import DocumentVersion

    async with async_session_factory() as db:
        doc = await db.get(Doc, doc_id)
        if doc is None:
            return
        kb_id = doc.kb_id
        stored_path = doc.stored_path
        await asyncio.to_thread(vector_store.delete_by_where, {"doc_id": doc_id})
        await db.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        # P0-8：清版本行（chunks 已被上面的 bulk delete 连带，这里清版本本身）
        await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == doc_id))
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
    同时级联清理该库沉淀的问答记忆（qa_memory），避免悬空记忆污染。
    """
    from app.db.models import Document as Doc
    from app.db.models import DocumentVersion
    from app.db.models import IndexVersion
    from app.db.models import KnowledgeBase as KB
    from app.db.models import QaMemory

    async with async_session_factory() as db:
        paths = (await db.execute(select(Doc.stored_path).where(Doc.kb_id == kb_id))).scalars().all()
        for p in paths:
            try:
                (settings.upload_dir_path / p).unlink(missing_ok=True)
            except Exception:
                pass
        await asyncio.to_thread(vector_store.delete_by_where, {"kb_id": kb_id})
        await db.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
        await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id.in_(
            select(Doc.id).where(Doc.kb_id == kb_id)
        )))
        await db.execute(delete(Doc).where(Doc.kb_id == kb_id))
        await db.execute(delete(QaMemory).where(QaMemory.kb_id == kb_id))
        await db.execute(delete(IndexVersion).where(IndexVersion.kb_id == kb_id))
        kb = await db.get(KB, kb_id)
        if kb is not None:
            await db.delete(kb)
        await db.commit()
    await asyncio.to_thread(bm25.remove_kb, kb_id)
    await semantic_cache.clear_cache()
