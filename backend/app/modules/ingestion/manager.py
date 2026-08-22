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
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.db.models import Chunk, Document, DocumentVersion, IngestionJob, KnowledgeBase
from app.db.session import async_session_factory
from app.services import bm25, semantic_cache, vector_store
from app.services.chunker import chunk_blocks, chunk_toc_pages
from app.services.embedding import embed_documents, load_cache_vectors, store_cache_vectors
from app.services.parser.factory import get_parser
from app.services.parser.ocr_progress import clear_progress

logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(2)
# P0-9：worker 常驻工作池。DB job 表是任务真相源——API 只写 queued job，worker 轮询
# 领取并处理；进程重启时 job 表仍在，启动后 worker 继续领剩下的（不会丢、不会重复）。
_running: dict[int, asyncio.Task] = {}
# 后台 worker 任务本身（供 lifespan 启动/停止；进程内常驻单实例即可）
_worker_task: asyncio.Task | None = None
# 优雅停止标记：置 True 后 worker 在「批间」退出，不再领新 job
_stop_requested = False
# 领任务轮询间隔（秒）：测试期间收紧，避免 sleep 拖慢
_claim_interval_s = 0.5
# 租约时长：worker 定期 heartbeat 续约；reaper 回收「lease 过期且未完成」的 job
_lease_seconds = 120
# 心跳间隔：worker 处理期间每 N 秒续一次租约（租约 1/3 时长，保证窗口内必续）。
# 大 PDF 解析可能远超初始租约，心跳是防止 reaper 误判 worker 死亡的关键。
_heartbeat_interval_s = max(_lease_seconds // 3, 1)
# 领到任务后 worker 单次处理一个 job，处理完立即回循环领下一个
# 内容去重 + 写入串行锁：并发入库时两个任务可能同时查 content_hash → 双双错过未提交的
# 插入 → UNIQUE 冲突（实测 doc3 因与其他扫描件产生相同短块哈希而入库失败）。加锁保证
# 「查重 + 写入」原子，同时避免 embedding 批处理与 Chroma 写盘的并发竞争。
_write_lock = asyncio.Lock()

# 活跃 stage（job 视为「未完成」；除此之外皆终态：succeeded/failed/cancelled）
_ACTIVE_STAGES = ("queued", "parsing", "chunking", "embedding", "indexing", "publishing", "verifying")


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


# 条款号：章节路径末尾的编号，如 "7.4 明渠均匀流" → "7.4"、"3.2 引用标准" → "3.2"
_CLAUSE_RE = re.compile(r"(\d+(?:\.\d+){1,3})(?:[ \t]|$)")
# 公式编号：正文中的 "(x.y.z-N)" 或 "式(x.y.z-N)" 结尾标记
_FORMULA_RE = re.compile(r"[（(](\d+(?:\.\d+){0,3}-\d+)[）)]")


def _extract_clause_no(section: str | None) -> str | None:
    """从章节路径提取条款号（如 "7.4 明渠均匀流 / 7.4.1 一般规定" → "7.4.1"）。"""
    if not section:
        return None
    # 取最后一个 / 之后的段
    last = section.split("/")[-1].strip()
    m = _CLAUSE_RE.search(last)
    return m.group(1) if m else None


def _extract_formula_no(content: str) -> str | None:
    """从块内容提取公式编号（如 "(7.4.3-1)" 或 "（5.48）"）。"""
    # 优先匹配带连字符的规范式编号 (x.y.z-N)
    m = _FORMULA_RE.search(content)
    if m:
        return m.group(1)
    # 回退：普通 "(x.y)" 编号（教材式，如 (3.45)）
    m2 = re.search(r"[（(](\d+\.\d{1,3})[）)]", content)
    return m2.group(1) if m2 else None


def enqueue_ingestion(doc_id: int) -> None:
    """投递入库任务：**写 DB job**（幂等：该文档已有活跃 job 则忽略，不重复投递）。

    P0-9 兼容 shim：同步签名（旧调用方 / 测试直接用），内部转异步写 job。
    生产路径 API 用 `enqueue_ingestion_async`（可直接 await，拿到 job_id）。
    """
    existing = _running.get(doc_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(enqueue_ingestion_async(doc_id, kind="ingest"))
    _running[doc_id] = task
    task.add_done_callback(lambda t: _running.pop(doc_id, None))


async def enqueue_ingestion_async(doc_id: int, kind: str = "ingest") -> int | None:
    """异步写 job（P0-9 主路径）：落库 queued job，返回 job_id；已有活跃 job 则跳过。

    幂等判定：该文档已存在未终态（活跃 stage）的 job → 直接返回 None（不重复投递）。
    """
    async with async_session_factory() as db:
        active = await db.scalar(
            select(IngestionJob).where(
                IngestionJob.document_id == doc_id,
                IngestionJob.stage.in_(_ACTIVE_STAGES),
            )
        )
        if active is not None:
            return None
        job = IngestionJob(document_id=doc_id, kind=kind, stage="queued")
        db.add(job)
        await db.commit()
        return job.id


def start_worker() -> None:
    """启动后台 worker 循环（幂等：已启动则不重复）。"""
    global _worker_task, _stop_requested
    if _worker_task is not None and not _worker_task.done():
        return
    _stop_requested = False
    _worker_task = asyncio.create_task(_worker_loop())


def stop_worker() -> None:
    """停止 worker 循环（幂等）：置停止标记并取消当前轮询/处理。

    供应用关闭与测试隔离使用。停止后 `start_worker()` 可再次拉起（重置标记）。
    """
    global _stop_requested, _worker_task
    _stop_requested = True
    task = _worker_task
    _worker_task = None
    if task is not None and not task.done():
        task.cancel()


async def _worker_loop() -> None:
    """worker 常驻循环：reaper 回收过期 job → 轮询领 queued job → 处理。

    每轮先跑 reaper（回收 lease 过期/worker 死亡的任务），再领新任务；
    空转节流：无任务时 sleep _claim_interval_s，避免对 SQLite 造成轮询压力。
    停止标记置位后，本轮处理完当前 job 就退出。
    """
    global _stop_requested
    while not _stop_requested:
        try:
            await _reaper_pass()
        except Exception:
            logger.exception("reaper 回收失败（忽略，下轮重试）")
        job_id = await _claim_next_job_async()
        if job_id is None:
            await asyncio.sleep(_claim_interval_s)
            continue
        async with _semaphore:
            await _execute_job(job_id)
    _stop_requested = False


async def _claim_next_job_async() -> int | None:
    """异步 CAS 领任务：把最早的 queued job 原子改 parsing（UPDATE..WHERE id+stage='queued'）。

    并发安全：先选出最早的 queued job id，再用「id + stage=queued」条件 UPDATE；
    只有命中行（rowcount==1）才真正领到。两个 worker 抢同一 job 时后到者 rowcount=0
    → 返回 None，下轮再抢别的，不会重复处理同一 job。
    """
    async with async_session_factory() as db:
        target = await db.scalar(
            select(IngestionJob.id)
            .where(IngestionJob.stage == "queued")
            .order_by(IngestionJob.id)
            .limit(1)
        )
        if target is None:
            return None
        now = _now()
        res = await db.execute(
            update(IngestionJob)
            .where(IngestionJob.id == target, IngestionJob.stage == "queued")
            .values(
                stage="parsing",
                attempt=IngestionJob.attempt + 1,
                lease_owner="worker",
                lease_until=now + timedelta(seconds=_lease_seconds),
                heartbeat_at=now,
            )
        )
        if res.rowcount == 0:
            return None  # 已被并发 worker 领走
        await db.commit()
        return target


async def _execute_job(job_id: int) -> None:
    """执行单个 job：驱动 _process_document（内部按 stage 回写 job）+ 终态落库。

    - 成功 → job.succeeded（stage 已由 _process_document 置位）
    - 失败 → job.failed + error_code/detail，doc.status=failed（旧 active 不受影响）
    - 取消 → job.cancelled + error_detail='cancelled'，doc 标 pending（下轮 reparse 可重入）
    """
    async with async_session_factory() as db:
        job = await db.get(IngestionJob, job_id)
        if job is None:
            return
        doc = await db.get(Document, job.document_id)
        if doc is None:
            job.stage = "failed"
            job.error_code = "DOC_MISSING"
            job.error_detail = "文档不存在"
            await db.commit()
            return
    try:
        # 心跳协程：处理期间每 _heartbeat_interval_s 续租一次，防止 reaper 误判 worker 死亡
        async def _heartbeat_loop():
            try:
                while True:
                    await asyncio.sleep(_heartbeat_interval_s)
                    async with async_session_factory() as db:
                        await _heartbeat_job(db, job_id)
            except asyncio.CancelledError:
                pass

        hb_task = asyncio.create_task(_heartbeat_loop())
        try:
            await _process_document(job.document_id)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
        async with async_session_factory() as db:
            job = await db.get(IngestionJob, job_id)
            if job is not None and job.stage in _ACTIVE_STAGES:
                # 取消竞态：_process_document 已发布成功，但取消请求在最后一个
                # _raise_if_cancelled 之后才到达 → 用户已明确取消，标记 cancelled，
                # 不标 succeeded（已发布的版本保留——回滚 = pointer 指回即可）
                if job.cancel_requested:
                    job.stage = "cancelled"
                    job.error_code = "CANCELLED"
                    job.error_detail = "用户取消（处理已完成后取消）"
                    doc = await db.get(Document, job.document_id)
                    if doc is not None:
                        doc.status = "pending"
                        doc.error_message = "用户取消入库"
                else:
                    job.stage = "succeeded"
                    job.lease_owner = None
                    job.lease_until = None
                await db.commit()
    except Exception as exc:
        logger.exception("入库失败 job=%s doc=%s", job_id, job.document_id)
        try:
            async with async_session_factory() as db:
                job = await db.get(IngestionJob, job_id)
                if job is None:
                    return
                # 取消：doc 标 pending（可重入重灌），job 标 cancelled；不动旧 active 版本
                cancelled = job.cancel_requested
                doc = await db.get(Document, job.document_id)
                if cancelled:
                    job.stage = "cancelled"
                    job.error_code = "CANCELLED"
                    job.error_detail = "用户取消"
                    if doc is not None:
                        doc.status = "pending"
                        doc.error_message = "用户取消入库"
                else:
                    job.stage = "failed"
                    job.error_code = str(exc)[:50]
                    job.error_detail = str(exc)[:1000]
                    if doc is not None:
                        doc.status = "failed"
                        doc.error_message = str(exc)[:1000]
                        # P0-8：失败只标 target 版本 failed，旧 active 版本不受影响
                        target = await db.scalar(
                            select(DocumentVersion).where(
                                DocumentVersion.document_id == job.document_id,
                                DocumentVersion.status == "building",
                            )
                        )
                        if target:
                            target.status = "failed"
                            target.error_message = str(exc)[:1000]
                clear_progress(doc.stored_path) if doc is not None else None
                job.lease_owner = None
                job.lease_until = None
                await db.commit()
        except Exception:
            logger.exception("回写失败状态失败 job=%s", job_id)


async def _heartbeat_job(db, job_id: int) -> None:
    """worker 心跳：续租（lease_until 顺延）+ 更新 heartbeat_at。

    复用调用方会话 db（与 _update_job_stage 同理，避免 SQLite 写锁竞争）。
    心跳只刷新租约，不动 stage。
    """
    job = await db.get(IngestionJob, job_id)
    if job is None or job.stage not in _ACTIVE_STAGES:
        return
    now = _now()
    job.lease_until = now + timedelta(seconds=_lease_seconds)
    job.heartbeat_at = now


async def _reaper_pass() -> None:
    """reaper 回收：把「租约过期且仍活跃」的 job 标 failed（worker 死亡/进程重启）。

    判定条件：stage 活跃 + lease_until 非空 + lease_until < now → 该 worker 已死，
    任务不可能自行完成 → 标 failed + 记错误；doc 标 failed（旧 active 版本不动）。
    - 只回收「有租约」的 job（queued 无租约 = 等 worker 领，不回收）
    - lease 未过期的活跃 job 一律不动（worker 还活着）
    """
    now = _now()
    async with async_session_factory() as db:
        stale = (
            await db.execute(
                select(IngestionJob).where(
                    IngestionJob.stage.in_(_ACTIVE_STAGES),
                    IngestionJob.lease_until.is_not(None),
                    IngestionJob.lease_until < now,
                )
            )
        ).scalars().all()
        for job in stale:
            logger.warning(
                "reaper 回收过期 job id=%s doc=%s stage=%s lease_until=%s",
                job.id, job.document_id, job.stage, job.lease_until,
            )
            job.stage = "failed"
            job.error_code = "LEASE_EXPIRED"
            job.error_detail = "worker 租约超时（进程中断或 worker 死亡），任务被回收"
            job.lease_owner = None
            job.lease_until = None
            doc = await db.get(Document, job.document_id)
            if doc is not None and doc.status in ("parsing", "embedding", "indexing"):
                doc.status = "failed"
                doc.error_message = "worker 租约超时，任务被回收"
        await db.commit()


class _JobCancelled(Exception):
    """协作式取消信号：worker 在解析/分块/嵌入批次间检测到 cancel_requested。"""


async def _raise_if_cancelled(db, doc_id: int) -> None:
    """协作式取消检查：job.cancel_requested 置位则抛 _JobCancelled（干净中断入库）。

    只在批次边界调用（解析后、分块后、嵌入后、索引前），不做细粒度 poll。
    **复用调用方的主会话 db**（不新开连接）：worker 处理期间主会话可能持有未提交
    写事务，新开会话会对 SQLite 写锁竞争（实测 database is locked）。
    """
    job = await db.scalar(
        select(IngestionJob).where(
            IngestionJob.document_id == doc_id,
            IngestionJob.stage.in_(_ACTIVE_STAGES),
        )
    )
    if job is not None and job.cancel_requested:
        raise _JobCancelled("用户取消入库")


async def _update_job_stage(db, doc_id: int, stage: str, **extra: Any) -> None:
    """把该文档活跃 job 的 stage 推进到指定阶段（解析/嵌入/索引等边界调用）。

    同样复用主会话 db，避免对 SQLite 写锁竞争。
    """
    job = await db.scalar(
        select(IngestionJob).where(
            IngestionJob.document_id == doc_id,
            IngestionJob.stage.in_(_ACTIVE_STAGES),
        )
    )
    if job is not None:
        job.stage = stage
        for k, v in extra.items():
            setattr(job, k, v)


async def cancel_ingestion(doc_id: int) -> bool:
    """协作式取消：置位该文档活跃 job 的 cancel_requested。

    返回是否真的置位了取消（有活跃 job 才返回 True）。worker 在批次边界检查后
    中断；若文档已在 queued（还没被领），可直接标 cancelled 避免 worker 领走。
    """
    async with async_session_factory() as db:
        job = await db.scalar(
            select(IngestionJob).where(
                IngestionJob.document_id == doc_id,
                IngestionJob.stage.in_(_ACTIVE_STAGES),
            )
        )
        if job is None:
            return False
        if job.stage == "queued":
            job.stage = "cancelled"
            job.error_code = "CANCELLED"
            job.error_detail = "用户取消"
            doc = await db.get(Document, doc_id)
            if doc is not None:
                doc.status = "pending"
                doc.error_message = "用户取消入库"
        else:
            job.cancel_requested = True
        await db.commit()
        return True


async def _run_ingestion(doc_id: int) -> None:
    """直接执行一次入库（兼容旧测试 / 脚本直接调用），不投递 job。

    供 test_version_lifecycle.py 等直接调 manager 的用例使用；生产路径走 job worker。
    """
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

        # P0-10 单元2：解析前二次验证（防 TOCTOU——上传时校验通过、解析前文件被换）。
        # 抛异常 → 走 _execute_job 失败路径：job failed + doc failed + 旧 active 版本保留。
        from app.modules.knowledge.upload_guard import verify_file

        verify_file(
            doc.file_type,
            settings.upload_dir_path / doc.stored_path,
        )

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

        # P0-9：解析完成 → job 推进到 chunking；批次间协作式取消检查
        await _update_job_stage(db, doc.id, "chunking")
        await _raise_if_cancelled(db, doc.id)

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
        # P0-9：分块完成 → job 推进到 embedding；批次间协作式取消检查
        await _update_job_stage(db, doc.id, "embedding")
        await _raise_if_cancelled(db, doc.id)
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

        # P0-9：嵌入+落库完成 → job 推进到 indexing；索引构建前最后取消检查
        await _update_job_stage(db, doc.id, "indexing")
        await _raise_if_cancelled(db, doc.id)

        # P0-8 影子索引：构建「新世界」快照（所有文档 active + 本 target）→ 核对 count →
        # 通过后再原子发布 pointer。任何失败走 _run_ingestion 失败路径，旧 active 不动。
        await _build_shadow_index(db, doc, target)
        # P0-9：索引就绪 → job 推进到 publishing（发布在即）
        await _update_job_stage(db, doc.id, "publishing")
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
        # P0-11 出处元数据：block_type（table/text）、clause_no（章节末尾条款号）、
        # formula_no（内容里的公式编号，如 (7.4.3-1)）；拿不到就 None。
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
                    block_type=getattr(c, "block_type", "text") or "text",
                    clause_no=_extract_clause_no(c.section),
                    formula_no=_extract_formula_no(c.content),
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
