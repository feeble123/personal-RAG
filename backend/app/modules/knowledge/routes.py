"""知识库管理路由（仅管理员）：知识库 CRUD + 文档上传/列表/删除/重解析 + 检索预览。"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from sqlalchemy import func, select

from app.core.config import settings
from app.core.deps import AdminUser, CurrentUser, DbSession, get_client_ip
from app.core.exceptions import BizError
from app.db.models import Chunk, Document, KnowledgeBase
from app.modules.ingestion import manager as ingestion
from app.modules.knowledge.schemas import (
    DocumentListOut,
    DocumentOut,
    KBCreate,
    KBOut,
    KBUpdate,
    SearchResult,
    UploadResult,
)
from app.services import audit, rag
from app.services.parser.ocr_progress import get_progress

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["knowledge"])

# 面向登录用户（非管理）的知识库列表：仅暴露名称与统计，供问答时选择知识库
public_router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


@public_router.get("", response_model=list[KBOut])
async def list_public_kbs(db: DbSession, _user: CurrentUser) -> list[KnowledgeBase]:
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.status != "empty").order_by(KnowledgeBase.created_at.desc())
    )
    return list(result.scalars().all())


# ---------------- 知识库 ----------------
@router.get("/kbs", response_model=list[KBOut])
async def list_kbs(db: DbSession, _admin: AdminUser) -> list[KnowledgeBase]:
    result = await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()))
    return list(result.scalars().all())


@router.post("/kbs", response_model=KBOut, status_code=201)
async def create_kb(
    body: KBCreate, db: DbSession, admin: AdminUser, request: Request
) -> KnowledgeBase:
    exists = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == body.name))
    if exists:
        raise BizError("知识库名称已存在", 409, "KB_EXISTS")
    kb = KnowledgeBase(
        name=body.name,
        description=body.description,
        answer_style=body.answer_style,
        created_by=admin.id,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    # P2-10：审计——创建知识库
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="kb.create",
        target_type="kb",
        target_id=str(kb.id),
        detail=f"创建知识库 {kb.name}",
        client_ip=await get_client_ip(request),
    )
    return kb


@router.patch("/kbs/{kb_id}", response_model=KBOut)
async def update_kb(kb_id: int, body: KBUpdate, db: DbSession, _admin: AdminUser) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise BizError("知识库不存在", 404, "KB_NOT_FOUND")
    if body.name and body.name != kb.name:
        exists = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == body.name))
        if exists:
            raise BizError("知识库名称已存在", 409, "KB_EXISTS")
        kb.name = body.name
    if body.description is not None:
        kb.description = body.description
    if body.answer_style is not None:
        kb.answer_style = body.answer_style
    await db.commit()
    await db.refresh(kb)
    return kb


@router.delete("/kbs/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: int, _db: DbSession, admin: AdminUser, request: Request
) -> None:
    kb = await _db.get(KnowledgeBase, kb_id)
    if not kb:
        raise BizError("知识库不存在", 404, "KB_NOT_FOUND")
    kb_name = kb.name
    await ingestion.delete_kb(kb_id)
    # P2-10：审计——删除知识库
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="kb.delete",
        target_type="kb",
        target_id=str(kb_id),
        detail=f"删除知识库 {kb_name}",
        client_ip=await get_client_ip(request),
    )


# ---------------- 文档 ----------------
@router.post("/kbs/{kb_id}/documents/upload", response_model=UploadResult, status_code=201)
async def upload_document(
    kb_id: int,
    db: DbSession,
    _admin: AdminUser,
    request: Request,
    file: UploadFile = File(...),
    chunk_strategy: str = Form(settings.chunk_strategy_default),
    doc_type: str = Form("other"),
) -> UploadResult:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise BizError("知识库不存在", 404, "KB_NOT_FOUND")

    # 切片策略（供 A/B 对比）：old=经典启发式 / new=目录+LLM断号补全；非法值回退默认
    if chunk_strategy not in ("old", "new"):
        chunk_strategy = settings.chunk_strategy_default

    # P0-11 文档类型（未来 DSH 引用来源判断）：textbook/standard/manual/other；非法值回退 other
    if doc_type not in ("textbook", "standard", "manual", "other"):
        doc_type = "other"

    original = file.filename or "untitled"
    ext = Path(original).suffix.lower().lstrip(".")
    if ext not in settings.allowed_extensions:
        raise BizError(
            f"不支持的文件格式 .{ext}，支持：{', '.join(settings.allowed_extensions)}",
            400,
            "UNSUPPORTED_FORMAT",
        )

    # P0-10 单元2：先进 quarantine 隔离区 → 校验通过 → 移入正式 uploads
    stored_name = f"{uuid4().hex}.{ext}"
    quarantine_path = settings.quarantine_dir_path / stored_name
    dest = settings.upload_dir_path / stored_name
    size = 0
    try:
        # 1. 流式写盘到隔离区（200MB 限制，不整文件进内存）
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        with quarantine_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_size:
                    raise BizError(
                        f"文件超过 {settings.max_upload_size // 1024 // 1024}MB 限制",
                        413,
                        "FILE_TOO_LARGE",
                    )
                out.write(chunk)

        # 2. 内容校验（伪造扩展名 / zip bomb / 二进制伪装）
        from app.modules.knowledge.upload_guard import verify_file

        verify_file(ext, quarantine_path)

        # 3. 校验通过 → 移入正式 uploads（同文件系统原子 move，无 TOCTOU 半成品）
        quarantine_path.replace(dest)
    except BizError:
        quarantine_path.unlink(missing_ok=True)  # 校验失败清理隔离区，不留垃圾
        raise

    doc = Document(
        kb_id=kb_id,
        filename=original,
        stored_path=stored_name,
        file_type=ext,
        file_size=size,
        status="pending",
        chunk_strategy=chunk_strategy,
        doc_type=doc_type,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # 后台入库（P0-9：写 DB job，worker 轮询执行；不 create_task）
    job_id = await ingestion.enqueue_ingestion_async(doc.id, kind="ingest")
    # 单元 J 单元⑤：返回排队位置（前面还有几个 queued 任务），让等待可解释
    queue_position = await ingestion.queued_ahead_count(job_id) if job_id else 0
    # P2-10：审计——上传文档
    await audit.record_audit(
        actor_id=_admin.id,
        actor_name=_admin.username,
        action="document.upload",
        target_type="document",
        target_id=str(doc.id),
        detail=f"上传文档 {original}（{doc_type}）",
        client_ip=await get_client_ip(request),
    )
    return UploadResult(id=doc.id, filename=original, status="pending", doc_type=doc_type, queue_position=queue_position)


@router.get("/kbs/{kb_id}/documents", response_model=DocumentListOut)
async def list_documents(
    kb_id: int,
    db: DbSession,
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> DocumentListOut:
    total = (await db.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb_id))) or 0
    rows = await db.execute(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for d in rows.scalars().all():
        out = DocumentOut.model_validate(d)
        if d.status == "parsing":
            p = get_progress(d.stored_path)
            if p:
                total_pages = p.get("total") or 0
                done = p.get("done") or 0
                out.progress = {
                    **p,
                    "percent": round(done / total_pages * 100, 1) if total_pages else None,
                }
        items.append(out)
    return DocumentListOut(items=items, total=total)


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def document_detail(doc_id: int, db: DbSession, _admin: AdminUser) -> Document:
    from sqlalchemy.orm import selectinload

    # P0-8：selectinload 预加载 versions（详情页展示重灌历史）
    doc = await db.get(Document, doc_id, options=[selectinload(Document.versions)])
    if not doc:
        raise BizError("文档不存在", 404, "DOC_NOT_FOUND")
    return doc


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: int, db: DbSession, _admin: AdminUser, request: Request
) -> None:
    doc = await db.get(Document, doc_id)
    if not doc:
        raise BizError("文档不存在", 404, "DOC_NOT_FOUND")
    filename = doc.filename
    await ingestion.delete_document(doc_id)
    # P2-10：审计——删除文档
    await audit.record_audit(
        actor_id=_admin.id,
        actor_name=_admin.username,
        action="document.delete",
        target_type="document",
        target_id=str(doc_id),
        detail=f"删除文档 {filename}",
        client_ip=await get_client_ip(request),
    )


@router.post("/documents/{doc_id}/reparse", response_model=DocumentOut)
async def reparse_document(
    doc_id: int, db: DbSession, _admin: AdminUser, request: Request
) -> Document:
    from sqlalchemy.orm import selectinload

    from app.db.models import DocumentVersion

    doc = await db.get(Document, doc_id, options=[selectinload(Document.versions)])
    if not doc:
        raise BizError("文档不存在", 404, "DOC_NOT_FOUND")
    # P0-8 并发保护：同一文档只允许一个 building 版本（另一个重灌在进行中）
    building = await db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == doc_id,
            DocumentVersion.status == "building",
        )
    )
    if building:
        raise BizError("该文档正在重新入库中，请稍后再试", 409, "REPARSE_IN_PROGRESS")
    doc.status = "pending"
    doc.error_message = None
    await db.commit()
    # P0-9：写 DB job（kind=reparse），worker 轮询执行
    await ingestion.enqueue_ingestion_async(doc_id, kind="reparse")
    await db.refresh(doc)
    # P2-10：审计——重解析文档
    await audit.record_audit(
        actor_id=_admin.id,
        actor_name=_admin.username,
        action="document.reparse",
        target_type="document",
        target_id=str(doc_id),
        detail=f"重解析文档 {doc.filename}",
        client_ip=await get_client_ip(request),
    )
    return doc


# ---------------- 检索预览（管理员验证库质量） ----------------
@router.get("/search", response_model=SearchResult)
async def search_preview(
    db: DbSession,
    _admin: AdminUser,
    q: str = Query(..., min_length=1, max_length=500),
    kb_id: int | None = Query(None),
    top_k: int = Query(5, ge=1, le=20),
) -> SearchResult:
    # 预览与问答一致：问题点名《书名》/「XXX中」时限定到该文档
    # P0-2 scope 隔离：书名解析限定当前库
    # P0-11：检索统一走 retriever（对外契约层），再转成前端 CitationOut 结构
    from app.schemas import CitationOut
    from app.services import retriever

    doc_ids = await rag.resolve_documents_by_title(db, q, kb_id=kb_id)
    results = await retriever.retrieve(q, top_k=top_k, kb_id=kb_id, doc_ids=doc_ids or None)
    hits = [
        CitationOut(
            chunk_id=r.source.chunk_id,
            doc_id=r.source.doc_id,
            source=r.source.document_name,
            page=r.source.page,
            section=r.source.section,
            snippet=r.text,
            score=r.score,
            rank=i + 1,
        )
        for i, r in enumerate(results)
    ]
    return SearchResult(hits=hits)


@router.get("/kbs/{kb_id}/chunks")
async def list_kb_chunks(
    kb_id: int,
    db: DbSession,
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    doc_id: int | None = Query(None, description="按文档筛选切片"),
):
    # P0-8 active 过滤：切片列表只显示当前可查版本（retired 不进列表，避免与已发布内容混淆）
    where = [Chunk.kb_id == kb_id]
    if doc_id is not None:
        where.append(Chunk.doc_id == doc_id)
    active_ids = (
        await db.scalars(
            select(Document.active_version_id).where(
                Document.kb_id == kb_id, Document.active_version_id.is_not(None)
            )
        )
    ).all()
    if active_ids:
        where.append(Chunk.document_version_id.in_(active_ids))
    total = (await db.scalar(select(func.count()).select_from(Chunk).where(*where))) or 0
    rows = await db.execute(
        select(Chunk)
        .where(*where)
        .order_by(Chunk.doc_id, Chunk.chunk_index)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "total": total,
        "items": [
            {
                "id": c.id,
                "doc_id": c.doc_id,
                "chunk_index": c.chunk_index,
                "page": c.page,
                "section": c.section,
                "content": c.content,
            }
            for c in rows.scalars().all()
        ],
    }


# ---------------- 入库任务（P0-9 持久化 job） ----------------
@router.get("/jobs", response_model=dict)
async def list_jobs(
    db: DbSession,
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """入库任务列表（管理员监控）：stage/attempt/progress/error/cancel 状态一览。"""
    from app.db.models import IngestionJob

    total = (await db.scalar(select(func.count()).select_from(IngestionJob))) or 0
    rows = await db.execute(
        select(IngestionJob)
        .order_by(IngestionJob.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for j in rows.scalars().all():
        items.append(
            {
                "id": j.id,
                "document_id": j.document_id,
                "kind": j.kind,
                "stage": j.stage,
                "attempt": j.attempt,
                "progress": j.progress,
                "error_code": j.error_code,
                "error_detail": j.error_detail,
                "cancel_requested": j.cancel_requested,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "updated_at": j.updated_at.isoformat() if j.updated_at else None,
            }
        )
    return {"total": total, "items": items}


@router.post("/documents/{doc_id}/cancel", status_code=200)
async def cancel_document_ingestion(doc_id: int, _db: DbSession, _admin: AdminUser) -> dict:
    """取消文档入库（协作式）：置位 cancel_requested，worker 在批次间中断。

    仅当存在活跃 job 时返回已取消；无任务/已终态返回 200 且 cancelled=False。
    """
    doc = await _db.get(Document, doc_id)
    if not doc:
        raise BizError("文档不存在", 404, "DOC_NOT_FOUND")
    ok = await ingestion.cancel_ingestion(doc_id)
    return {"cancelled": ok}
