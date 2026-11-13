"""本体（Ontology RAG）管理接口 —— 只有管理员能用。

路径前缀 `/api/admin/ontology`：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET    | /types              | 查看当前 TBox 有哪些实体/关系 |
| GET    | /instances          | 列出本体实例（可按类型/库/状态筛） |
| POST   | /instances          | 新建实例（按 TBox 校验） |
| PATCH  | /instances/{id}     | 修改实例 |
| DELETE | /instances/{id}     | 删除实例 |
| GET    | /params             | 参数推荐卡（可带流域条件收窄） |
| POST   | /params/import      | 上传参数表 Excel（默认只预演） |
| POST   | /profile/reload     | 重载 YAML 本体 |

约定：
- 写操作全部落审计（action 以 `ontology.` 开头）；
- 数值校验不过 → 400，不做「先写进去再说」；
- 参数表导入默认 dry_run=true，与命令行脚本行为一致。
"""
from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import AdminUser, DbSession, get_client_ip, verify_external_api_key
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.core.time import utcnow
from app.modules.ontology.schemas import (
    AskIn,
    AttributeIn,
    CardOut,
    EntityTypeIn,
    ExtractRunIn,
    ImportResult,
    InstanceCreate,
    InstanceOut,
    InstancePatch,
    LibraryCreate,
    LibraryOut,
    ProposalAcceptIn,
    ProposalOut,
    RelationCreate,
    RelationOut,
    RelationPatch,
    RelationTypeIn,
    ReloadOut,
)
from app.services import audit
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology import libraries as lib_mod
from app.services.graph.ontology import queries
from app.services.graph.ontology import excel_schema
from app.services.graph.ontology import extraction
from app.services.graph.ontology import instance_import
from app.services.graph.ontology import relations as rel_mod
from app.services.graph.ontology import tbox_edit
from app.services.graph.ontology.params import load_prior_excel, resolve_evidence
from app.services.graph.ontology.registry import get_registry
from app.services.graph.ontology.retrieval import retrieve_with_ontology
from app.services.graph.ontology.validator import validate_entity, validate_relation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/ontology", tags=["ontology"])
# 对外接口（agent 用）：X-API-Key 鉴权，与 /api/v1/search 同一套密钥
public_router = APIRouter(
    prefix="/v1/ontology",
    tags=["ontology"],
    dependencies=[Depends(verify_external_api_key)],
)

_MAX_IMPORT_BYTES = 10 * 1024 * 1024  # 10MB，参数表远小于这个
_ENTITY_TYPE_DEFAULT = "Parameter"


def _registry(profile: str | None):
    """TBox 解析：实现在 service 层，MCP 用的是同一个。"""
    return queries.resolve_profile(profile)


def _relation_edge(rel) -> dict:
    """PG 关系行 → 前端画图用的边。"""
    evidence = [e for e in (rel.evidence or []) if isinstance(e, dict)]
    return {
        "id": f"pg:{rel.id}",
        "source": rel.subject_uid,
        "target": rel.object_uid,
        "predicate": rel.predicate,
        "status": rel.status,
        "confidence": 1.0,
        "evidence_text": "；".join(str(e.get("note") or "") for e in evidence if e.get("note")),
        "chunk_ids": [e["chunk_id"] for e in evidence if e.get("chunk_id") is not None],
    }


async def _sync_graph(*, nodes: list | None = None, edges: list | None = None,
                      drop_nodes: list[str] | None = None,
                      drop_edges: list[tuple[str, str, str]] | None = None) -> None:
    """管理端写完 PG 后把变化推给图库（尽力而为，失败只记日志）。

    PG 是真相源，图库只是索引：这里失败不会让写操作失败，
    下次重放（导入脚本或再改一次）就会补上。
    """
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        return
    try:
        if drop_edges:
            for subject, predicate, obj in drop_edges:
                await store.delete_edges(subject, predicate, obj)
        if drop_nodes:
            await store.delete_nodes(drop_nodes)
        if nodes:
            await store.upsert(nodes, edges or [])
        elif edges:
            await store.upsert([], edges)
    except Exception as exc:  # noqa: BLE001 - 索引失败不影响真相源
        logger.warning("图库同步失败（PG 已保存，稍后重放即可）：%s", exc)


@router.get("/types")
async def get_types(_admin: AdminUser, profile: str | None = Query(None)) -> dict:
    """TBox 概览：有哪些实体类型、关系类型、各自的属性。"""
    return _registry(profile).to_dict()


@router.post("/profile/reload", response_model=ReloadOut)
async def reload_profile(
    admin: AdminUser, request: Request, profile: str | None = Query(None)
) -> ReloadOut:
    """改了 YAML 之后热重载（不需要重启服务）。"""
    wanted = profile or settings.ontology_profile
    try:
        reg = get_registry(wanted, reload=True)
    except Exception as exc:  # noqa: BLE001
        raise BizError(f"本体 profile 加载失败：{exc}", 400, "ONTOLOGY_PROFILE_INVALID") from exc
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.profile.reload",
        target_type="ontology_profile",
        target_id=reg.profile_id,
        detail=f"实体 {len(reg.entity_types)} 类 / 关系 {len(reg.relation_types)} 种",
        client_ip=await get_client_ip(request),
    )
    return ReloadOut(
        profile_id=reg.profile_id,
        entity_types=len(reg.entity_types),
        relation_types=len(reg.relation_types),
    )


# ---------------- 自动抽取（F4）：规则先跑 → LLM 补漏 → 人工确认 ----


async def _proposal_out(db: AsyncSession, rows: list) -> list[ProposalOut]:
    """补上实例中文名——审核列表上光看 uid 没法判断。"""
    from app.db.models import OntologyInstance

    uids = {r.instance_uid for r in rows}
    names: dict[str, str] = {}
    if uids:
        for inst in (
            await db.scalars(select(OntologyInstance).where(OntologyInstance.uid.in_(uids)))
        ).all():
            names[inst.uid] = str((inst.props or {}).get("display_zh") or inst.name)
    return [
        ProposalOut(
            id=r.id,
            profile_id=r.profile_id,
            instance_uid=r.instance_uid,
            instance_name=names.get(r.instance_uid, r.instance_uid),
            entity_type=r.entity_type,
            attribute=r.attribute,
            value=r.value,
            method=r.method,
            confidence=float(r.confidence or 0),
            evidence=r.evidence,
            status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/extract/run")
async def run_extraction(
    payload: ExtractRunIn, db: DbSession, admin: AdminUser, request: Request
) -> dict:
    """扫知识库，给实例找属性值，结果进待审区（不直接改实例）。"""
    reg = _registry(payload.profile_id)

    proposals = await extraction.run_rule_extraction(db, reg, kb_id=payload.kb_id)
    rule_hits = {(p.instance_uid, p.attribute) for p in proposals}
    llm_hits = 0
    if payload.use_llm:
        llm = await extraction.run_llm_extraction(
            db, reg, kb_id=payload.kb_id, skip=rule_hits
        )
        llm_hits = len(llm)
        proposals.extend(llm)

    saved = 0 if payload.dry_run else await extraction.save_proposals(db, reg.profile_id, proposals)
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.extract.run",
        target_type="ontology_profile",
        target_id=reg.profile_id,
        detail=f"kb={payload.kb_id} 规则 {len(rule_hits)} / LLM {llm_hits} / 新入库 {saved}",
        client_ip=await get_client_ip(request),
    )
    return {
        "profile_id": reg.profile_id,
        "rule_hits": len(rule_hits),
        "llm_hits": llm_hits,
        "saved": saved,
        "dry_run": payload.dry_run,
        "proposals": [
            {
                "instance_uid": p.instance_uid,
                "entity_type": p.entity_type,
                "attribute": p.attribute,
                "value": p.value,
                "method": p.method,
                "confidence": p.confidence,
                "evidence": p.evidence,
            }
            for p in proposals[:200]
        ],
    }


@router.get("/proposals", response_model=list[ProposalOut])
async def list_proposals(
    db: DbSession,
    _admin: AdminUser,
    profile: str | None = Query(None),
    status: str = Query("pending", pattern="^(pending|accepted|rejected|all)$"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[ProposalOut]:
    from app.db.models import OntologyInstance, OntologyProposal

    reg = _registry(profile)
    stmt = select(OntologyProposal).where(OntologyProposal.profile_id == reg.profile_id)
    if status != "all":
        stmt = stmt.where(OntologyProposal.status == status)
    rows = (await db.scalars(stmt.order_by(OntologyProposal.id.desc()).limit(limit))).all()
    return await _proposal_out(db, list(rows))


async def _review(
    db: AsyncSession, ids: list[int], *, accept: bool
) -> dict:
    from app.db.models import OntologyInstance, OntologyProposal

    rows = (await db.scalars(select(OntologyProposal).where(OntologyProposal.id.in_(ids)))).all()
    done, missing = 0, 0
    touched_nodes: list = []
    for row in rows:
        if row.status != "pending":
            continue
        if accept:
            ok = await extraction.accept_proposal(db, row)
            if not ok:
                missing += 1  # 实例已被删掉：提案失去意义，直接标记
            else:
                from app.services.graph.ontology import instances as inst_mod

                inst = await inst_mod.get_instance_by_uid(db, row.instance_uid)
                if inst is not None:
                    touched_nodes.append(inst_mod.to_node(inst))
        row.status = "accepted" if accept else "rejected"
        row.reviewed_at = utcnow()
        done += 1
    await db.commit()
    if touched_nodes:
        await _sync_graph(nodes=touched_nodes)
    return {"reviewed": done, "skipped": len(ids) - done, "missing_instance": missing}


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(
    proposal_id: int, db: DbSession, admin: AdminUser, request: Request
) -> dict:
    result = await _review(db, [proposal_id], accept=True)
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.proposal.accept",
        target_type="ontology_proposal", target_id=str(proposal_id),
        client_ip=await get_client_ip(request),
    )
    return result


@router.post("/proposals/accept")
async def accept_proposals(
    payload: ProposalAcceptIn, db: DbSession, admin: AdminUser, request: Request
) -> dict:
    result = await _review(db, payload.ids, accept=True)
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.proposal.accept_batch",
        target_type="ontology_proposal", target_id=f"{len(payload.ids)} 条",
        client_ip=await get_client_ip(request),
    )
    return result


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: int, db: DbSession, admin: AdminUser, request: Request
) -> dict:
    result = await _review(db, [proposal_id], accept=False)
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.proposal.reject",
        target_type="ontology_proposal", target_id=str(proposal_id),
        client_ip=await get_client_ip(request),
    )
    return result


# ---------------- 图谱库（一个库 = 一份 TBox + 属于它的实例/关系）----


async def _library_out(
    db: AsyncSession, info: lib_mod.LibraryInfo, graph_stats: dict | None = None
) -> LibraryOut:
    """补上库里有多少实例/关系——删库前的判断依据，也是界面上的库简介。

    `node_count` 取**图里真实节点数**（按本库声明的实体类型收口）：
    只数 PG 实例会让「防洪链本体」显示 0（那些节点是历史导入的，没有实例行），
    用户切进去却看见 25 个节点，对不上。
    """
    from sqlalchemy import func, select

    from app.db.models import OntologyInstance, OntologyRelation

    instances = await db.scalar(
        select(func.count())
        .select_from(OntologyInstance)
        .where(OntologyInstance.profile_id == info.profile_id)
    )
    relations = await db.scalar(
        select(func.count())
        .select_from(OntologyRelation)
        .where(OntologyRelation.profile_id == info.profile_id)
    )
    node_count = 0
    if graph_stats:
        by_type = (graph_stats.get("nodes") or {}).get("by_type") or {}
        node_count = sum(int(by_type.get(t, 0)) for t in info.entity_types)
    return LibraryOut(
        **info.to_dict(),
        instance_count=int(instances or 0),
        relation_count=int(relations or 0),
        node_count=node_count,
        is_default=info.profile_id == settings.ontology_profile,
    )


@router.get("/libraries", response_model=list[LibraryOut])
async def list_libraries(db: DbSession, _admin: AdminUser) -> list[LibraryOut]:
    """列出所有图谱库（扫 profiles 目录，坏库也会列出来并带 error）。"""
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    stats = await store.stats() if store is not None else None
    return [await _library_out(db, info, stats) for info in lib_mod.list_libraries()]


@router.get("/libraries/{profile_id}/export")
async def export_library(profile_id: str, _admin: AdminUser) -> Response:
    """导出图谱库的 TBox YAML（换机器带走 / 交给同事 / 进版本库）。"""
    try:
        filename, text = lib_mod.read_library_text(profile_id)
    except Exception as exc:  # noqa: BLE001
        raise BizError(str(exc), 404, "ONTOLOGY_LIBRARY_NOT_FOUND") from exc
    # 用 Response 直接把文本回给浏览器；FileResponse 只认磁盘路径，不认内存流
    return Response(
        content=text.encode("utf-8"),
        media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/libraries/import", response_model=LibraryOut, status_code=201)
async def import_library(
    db: DbSession,
    admin: AdminUser,
    request: Request,
    file: UploadFile = File(..., description="TBox YAML 文件"),
) -> LibraryOut:
    """导入一份现成的 TBox YAML（不覆盖同名库）。"""
    raw = await file.read()
    if len(raw) > _MAX_IMPORT_BYTES:
        raise BizError("文件过大", 413, "PAYLOAD_TOO_LARGE")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BizError("YAML 必须是 UTF-8 编码", 400, "ONTOLOGY_LIBRARY_INVALID") from exc
    try:
        info = lib_mod.import_library(text)
    except Exception as exc:  # noqa: BLE001
        raise BizError(str(exc), 400, "ONTOLOGY_LIBRARY_INVALID") from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.library.import",
        target_type="ontology_profile", target_id=info.profile_id,
        detail=f"导入 {info.display_name}",
        client_ip=await get_client_ip(request),
    )
    return await _library_out(db, info)


@router.post("/schema/excel-preview")
async def excel_preview(
    _admin: AdminUser,
    file: UploadFile = File(..., description="Excel 文件，读第一行表头"),
) -> dict:
    """读 Excel 表头，给出「这些列可以建成哪些属性」的建议（只预览，不写入）。"""
    raw = await file.read()
    if len(raw) > _MAX_IMPORT_BYTES:
        raise BizError("文件过大", 413, "PAYLOAD_TOO_LARGE")
    try:
        columns = excel_schema.suggest_columns(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - 用户上传的文件，任何解析错误都要可读
        raise BizError(f"读不出表头：{exc}", 400, "ONTOLOGY_EXCEL_INVALID") from exc
    return {"columns": [c.to_dict() for c in columns]}


@router.post("/libraries", response_model=LibraryOut, status_code=201)
async def create_library(
    payload: LibraryCreate, db: DbSession, admin: AdminUser, request: Request
) -> LibraryOut:
    """新建图谱库：复制一份 TBox 模板（或空白库），实例/关系从零开始。"""
    try:
        info = lib_mod.create_library(
            payload.profile_id,
            payload.display_name,
            payload.description,
            template=payload.template,
        )
    except Exception as exc:  # noqa: BLE001 - YAML 也属于用户输入，给可读提示
        raise BizError(str(exc), 400, "ONTOLOGY_LIBRARY_INVALID") from exc
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.library.create",
        target_type="ontology_profile",
        target_id=info.profile_id,
        detail=f"{info.display_name}（模板 {payload.template}）",
        client_ip=await get_client_ip(request),
    )
    return await _library_out(db, info)


@router.delete("/libraries/{profile_id}", status_code=204)
async def delete_library(
    profile_id: str, db: DbSession, admin: AdminUser, request: Request
) -> None:
    """删除图谱库。库里有实例或关系时拒绝——先清空再删，避免误删数据。"""
    if not lib_mod.library_exists(profile_id):
        raise BizError(f"图谱库 {profile_id!r} 不存在", 404, "ONTOLOGY_LIBRARY_NOT_FOUND")
    info = next(
        (i for i in lib_mod.list_libraries() if i.profile_id == profile_id), None
    )
    if info is not None:
        counted = await _library_out(db, info)
        if counted.instance_count or counted.relation_count:
            raise BizError(
                f"图谱库「{counted.display_name}」里还有 "
                f"{counted.instance_count} 个实例、{counted.relation_count} 条关系，"
                "请先清空再删除",
                409,
                "ONTOLOGY_LIBRARY_NOT_EMPTY",
            )
    try:
        lib_mod.delete_library(profile_id)
    except Exception as exc:  # noqa: BLE001
        raise BizError(str(exc), 400, "ONTOLOGY_LIBRARY_INVALID") from exc
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.library.delete",
        target_type="ontology_profile",
        target_id=profile_id,
        detail="删除图谱库",
        client_ip=await get_client_ip(request),
    )


# ---------------- TBox 在线编辑（F3）----------------


def _tbox_error(exc: Exception) -> BizError:
    return BizError(str(exc), 400, "ONTOLOGY_TBOX_INVALID")


@router.post("/schema/entity")
async def upsert_entity_type(
    payload: EntityTypeIn,
    admin: AdminUser,
    request: Request,
    profile: str | None = Query(None),
) -> dict:
    """新建或修改实体类型（写回 YAML）。"""
    reg = _registry(profile)
    try:
        result = tbox_edit.upsert_entity_type(
            reg.profile_id,
            tbox_edit.EntityTypeInput(
                name=payload.name,
                display_name=payload.display_name,
                uid_prefix=payload.uid_prefix,
                scope=payload.scope,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise _tbox_error(exc) from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.entity.upsert",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"{payload.display_name}（{payload.name}）",
        client_ip=await get_client_ip(request),
    )
    return result


@router.delete("/schema/entity/{name}")
async def delete_entity_type(
    name: str, db: DbSession, admin: AdminUser, request: Request,
    profile: str | None = Query(None),
) -> dict:
    """删除实体类型。被关系引用 / 已有实例时拒绝。"""
    from sqlalchemy import func, select

    from app.db.models import OntologyInstance

    reg = _registry(profile)
    count = await db.scalar(
        select(func.count())
        .select_from(OntologyInstance)
        .where(
            OntologyInstance.profile_id == reg.profile_id,
            OntologyInstance.entity_type == name,
        )
    )
    try:
        result = tbox_edit.delete_entity_type(
            reg.profile_id, name, instance_count=int(count or 0)
        )
    except Exception as exc:  # noqa: BLE001
        raise _tbox_error(exc) from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.entity.delete",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"删除实体类型 {name}",
        client_ip=await get_client_ip(request),
    )
    return result


@router.post("/schema/relation")
async def upsert_relation_type(
    payload: RelationTypeIn,
    admin: AdminUser,
    request: Request,
    profile: str | None = Query(None),
) -> dict:
    """新建或修改关系类型（写回 YAML）。"""
    reg = _registry(profile)
    try:
        result = tbox_edit.upsert_relation_type(
            reg.profile_id,
            tbox_edit.RelationTypeInput(
                name=payload.name,
                display_name=payload.display_name,
                source=payload.source,
                target=payload.target,
                target_types=tuple(payload.target_types),
                description=payload.description,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise _tbox_error(exc) from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.relation.upsert",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"{payload.display_name}（{payload.name}）",
        client_ip=await get_client_ip(request),
    )
    # 改了主语/宾语方向后，已有边还是老方向——这里报个数，让前端问一句要不要一起掉头。
    reversed_rows = await rel_mod.find_reversed_edges(
        db,
        profile_id=reg.profile_id,
        predicate=payload.name,
        source_type=payload.source,
        target_type=payload.target,
    )
    return {"types": result, "reversed_edges": len(reversed_rows)}


@router.post("/schema/relation/{name}/fix-reversed")
async def fix_reversed_edges(
    name: str, db: DbSession, admin: AdminUser, request: Request,
    profile: str | None = Query(None),
) -> dict:
    """按新的关系定义，把方向相反的已有边掉头。"""
    reg = _registry(profile)
    rel = reg.relation(name)
    if rel is None:
        raise BizError(f"关系类型 {name!r} 不存在", 404, "ONTOLOGY_RELATION_NOT_FOUND")
    rows = await rel_mod.find_reversed_edges(
        db,
        profile_id=reg.profile_id,
        predicate=name,
        source_type=rel.source,
        target_type=rel.target,
    )
    drops = [(r.subject_uid, r.predicate, r.object_uid) for r in rows]
    fixed = await rel_mod.swap_endpoints(db, rows)
    # 图库：删旧边、写新边（PG 已是真相源，这里失败也只是索引滞后）
    await _sync_graph(
        drop_edges=drops,
        edges=[rel_mod.to_edge(r) for r in rows],
    )
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.relation.fix_reversed",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"{name} 掉头 {fixed} 条",
        client_ip=await get_client_ip(request),
    )
    return {"fixed": fixed}


@router.delete("/schema/relation/{name}")
async def delete_relation_type(
    name: str, db: DbSession, admin: AdminUser, request: Request,
    profile: str | None = Query(None),
) -> dict:
    """删除关系类型。已有边在用时拒绝。"""
    from sqlalchemy import func, select

    from app.db.models import OntologyRelation

    reg = _registry(profile)
    count = await db.scalar(
        select(func.count())
        .select_from(OntologyRelation)
        .where(
            OntologyRelation.profile_id == reg.profile_id,
            OntologyRelation.predicate == name,
        )
    )
    try:
        result = tbox_edit.delete_relation_type(
            reg.profile_id, name, edge_count=int(count or 0)
        )
    except Exception as exc:  # noqa: BLE001
        raise _tbox_error(exc) from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.relation.delete",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"删除关系类型 {name}",
        client_ip=await get_client_ip(request),
    )
    return result


@router.post("/schema/entity/{name}/attribute")
async def upsert_attribute(
    name: str, payload: AttributeIn, admin: AdminUser, request: Request,
    profile: str | None = Query(None),
) -> dict:
    """新建或修改某实体类型的属性（写回 YAML）。"""
    reg = _registry(profile)
    try:
        result = tbox_edit.upsert_attribute(
            reg.profile_id,
            name,
            tbox_edit.AttributeInput(
                name=payload.name,
                type=payload.type,
                values=tuple(payload.values),
                required=payload.required,
                description=payload.description,
                extract=(
                    {
                        "pattern": payload.extract_pattern,
                        **({"unit": payload.extract_unit} if payload.extract_unit else {}),
                        **({"map": payload.extract_map} if payload.extract_map else {}),
                    }
                    if payload.extract_pattern
                    else None
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise _tbox_error(exc) from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.attribute.upsert",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"{name}.{payload.name}（{payload.type}）",
        client_ip=await get_client_ip(request),
    )
    return result


@router.delete("/schema/entity/{name}/attribute/{attr_name}")
async def delete_attribute(
    name: str, attr_name: str, admin: AdminUser, request: Request,
    profile: str | None = Query(None),
) -> dict:
    """删除属性。必填属性拒绝直接删（会让已有实例不合规）。"""
    reg = _registry(profile)
    try:
        result = tbox_edit.delete_attribute(reg.profile_id, name, attr_name)
    except Exception as exc:  # noqa: BLE001
        raise _tbox_error(exc) from exc
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.tbox.attribute.delete",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"删除属性 {name}.{attr_name}",
        client_ip=await get_client_ip(request),
    )
    return result


@router.get("/graph")
async def get_graph(
    _admin: AdminUser,
    db: DbSession,
    profile: str | None = Query(None),
    entity_type: list[str] | None = Query(None, description="只取这些实体类型，可重复传"),
    kb_id: int | None = Query(None),
    node_limit: int = Query(300, ge=1, le=1000),
    edge_limit: int = Query(1000, ge=1, le=3000),
) -> dict:
    """前端画图用的全览：节点 + 边。

    节点以 **PG 实例**为准（图库只是索引，没开图库时参数照样看得见），
    边只能来自图库（关系目前只存在于图里，F2 会把关系的真相源也落到 PG）。
    """
    from app.services.graph.store import get_graph_store

    reg = _registry(profile)
    rows = await inst_mod.list_instances(
        db,
        profile_id=reg.profile_id,
        entity_type=None if not entity_type or len(entity_type) > 1 else entity_type[0],
        kb_id=kb_id,
        status="active",
        limit=node_limit,
    )
    if entity_type and len(entity_type) > 1:
        rows = [r for r in rows if r.entity_type in entity_type]

    nodes: dict[str, dict] = {}
    for row in rows:
        props = row.props or {}
        nodes[row.uid] = {
            "uid": row.uid,
            "name": row.name,
            "display": props.get("display_zh") or row.name,
            "entity_type": row.entity_type,
            "kb_id": row.kb_id,
            "props": props,
            "evidence_count": len(row.evidence or []),
            "source": "instance",
        }

    store = get_graph_store()
    truncated = len(rows) >= node_limit

    # PG 里的关系是真相源：图库没开 / 还没同步时，这些边照样要能画出来
    edge_by_key: dict[tuple[str, str, str], dict] = {}
    for rel in await rel_mod.list_relations(
        db, profile_id=reg.profile_id, kb_id=kb_id, status="active", limit=edge_limit
    ):
        edge_by_key[(rel.subject_uid, rel.predicate, rel.object_uid)] = _relation_edge(rel)

    if store is None:
        return {
            "profile_id": reg.profile_id,
            "nodes": list(nodes.values()),
            "edges": list(edge_by_key.values()),
            "truncated": truncated,
            "graph_available": False,
            "note": "图库未启用（GRAPH_ENABLED=false）；节点来自本体实例，关系边来自 PG 人工维护的关系",
            "type_labels": _type_labels(nodes.values(), reg),
            "relation_labels": _relation_labels(reg),
        }

    # 图库是全库共享的，必须按**当前库声明过的实体类型**收口，
    # 否则切到别的图谱库还会看到这一库的节点（用户实测：切库后仍是 38 节点）。
    declared = list(reg.entity_types)
    store_types = [t for t in (entity_type or declared) if t in reg.entity_types]
    slice_ = await store.overview(
        entity_types=store_types,
        kb_id=kb_id,
        node_limit=node_limit,
        edge_limit=edge_limit,
    )
    for node in slice_.nodes:
        if node.uid in nodes:
            continue
        nodes[node.uid] = {
            "uid": node.uid,
            "name": node.name,
            "display": node.name,
            "entity_type": node.type_name,
            "kb_id": node.kb_ids[0] if node.kb_ids else 0,
            "props": node.props,
            "evidence_count": 0,
            "source": "graph",
        }
    # 图库里的边补进来（含自动抽取的），与 PG 的关系按 (主语,关系,宾语) 去重
    for e in slice_.edges:
        key = (e.subject_uid, e.predicate_name, e.object_uid)
        if key in edge_by_key:
            continue
        edge_by_key[key] = {
            "id": f"{e.subject_uid}|{e.predicate_name}|{e.object_uid}|{e.version_id}",
            "source": e.subject_uid,
            "target": e.object_uid,
            "predicate": e.predicate_name,
            "status": e.status,
            "confidence": e.confidence,
            "evidence_text": e.evidence_text,
            "chunk_ids": e.chunk_ids,
        }
    edges = list(edge_by_key.values())

    return {
        "profile_id": reg.profile_id,
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated or slice_.truncated,
        "graph_available": True,
        "type_labels": _type_labels(nodes.values(), reg),
        "relation_labels": _relation_labels(reg),
    }


def _relation_labels(reg) -> dict[str, str]:
    """关系类型 → 中文名（PART_OF → 属于）。界面上一律显示中文。

    必须**同时**覆盖两套关系：
    - 内置防洪链枚举（downstream_of / located_in / responsible …）——存量边用的是这套；
    - 当前 TBox 里声明的关系（PART_OF / AFFECTS …）——新建的边用的是这套。
    只取 TBox 会让内置关系在界面上露英文（用户实测：青溪水库 —downstream_of→ 河段）。
    """
    from app.services.graph.schema import RELATION_LABELS_ZH

    labels: dict[str, str] = {rt.value: zh for rt, zh in RELATION_LABELS_ZH.items()}
    labels.update({
        name: (rel.display_name or rel.description or name)
        for name, rel in reg.relation_types.items()
    })
    return labels


def _type_labels(nodes, reg) -> dict[str, str]:
    """实体类型 → 中文名。图例和分组标签要给人看，不能直接显示 engineering_object。"""
    from app.services.graph.schema import entity_label_zh

    labels: dict[str, str] = {}
    for node in nodes:
        entity_type = node["entity_type"]
        if entity_type in labels:
            continue
        declared = reg.entity(entity_type)
        labels[entity_type] = (
            getattr(declared, "display_name", None) or entity_label_zh(entity_type) or entity_type
        )
    return labels


@router.get("/instances", response_model=list[InstanceOut])
async def list_instances(
    _admin: AdminUser,
    db: DbSession,
    profile: str | None = Query(None),
    entity_type: str | None = Query(None),
    kb_id: int | None = Query(None, description="不传=全部；0=全局"),
    status: str | None = Query("active", description="active / archived / all"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[InstanceOut]:
    rows = await inst_mod.list_instances(
        db,
        profile_id=profile or settings.ontology_profile,
        entity_type=entity_type,
        kb_id=kb_id,
        status=None if status in (None, "all") else status,
        limit=limit,
    )
    return [InstanceOut.model_validate(r) for r in rows]


@router.post("/instances/import")
async def import_instances(
    db: DbSession,
    admin: AdminUser,
    request: Request,
    file: UploadFile = File(..., description="Excel：表头=属性名或属性的中文说明"),
    entity_type: str = Query(..., description="这批数据属于哪个实体类型"),
    kb_id: int = Query(0, ge=0),
    profile: str | None = Query(None),
    dry_run: bool = Query(True, description="默认只预演，确认无误再改成 false"),
) -> dict:
    """一张表批量建实例。默认预演；正式导入会按 (类型, 名称) 幂等更新。"""
    from app.db.models import OntologyInstance

    reg = _registry(profile)
    raw = await file.read()
    if len(raw) > _MAX_IMPORT_BYTES:
        raise BizError("文件过大", 413, "PAYLOAD_TOO_LARGE")
    try:
        preview = instance_import.preview_import(io.BytesIO(raw), reg, entity_type)
    except Exception as exc:  # noqa: BLE001 - 用户上传的表，什么错都要给可读提示
        raise BizError(str(exc), 400, "ONTOLOGY_EXCEL_INVALID") from exc

    if not dry_run and preview.total > 0 and not preview.rows and preview.errors:
        # 正式导入且一行都没成：直接抛出来，别写库。
        # 预演时**不抛**——用户需要看到"哪些列没对上、哪些行有问题"才知道怎么改表。
        raise BizError(
            f"没有可导入的行（{len(preview.errors)} 行有问题）："
            f"{preview.errors[0].get('message', '')}",
            400,
            "ONTOLOGY_EXCEL_INVALID",
        )

    result = {
        "entity_type": entity_type,
        "dry_run": dry_run,
        "total": preview.total,
        "valid": len(preview.rows),
        "rejected": len(preview.errors),
        "mapped": preview.mapped,
        "skipped_headers": preview.skipped_headers,
        "errors": preview.errors,
    }
    if dry_run:
        result["rows"] = preview.to_dict()["rows"]
        return result

    drafts = [
        inst_mod.InstanceDraft(
            profile_id=reg.profile_id,
            entity_type=entity_type,
            name=row.name,
            uid=reg.uid_for(entity_type, row.name, kb_id),
            kb_id=kb_id,
            props=row.props,
            source="import",
        )
        for row in preview.rows
    ]
    upserted = await inst_mod.upsert_instances(db, drafts)
    await db.commit()
    rows = [
        r
        for r in (
            await db.scalars(
                select(OntologyInstance).where(
                    OntologyInstance.profile_id == reg.profile_id,
                    OntologyInstance.entity_type == entity_type,
                )
            )
        ).all()
    ]
    await _sync_graph(nodes=[inst_mod.to_node(r) for r in rows])
    await audit.record_audit(
        actor_id=admin.id, actor_name=admin.username,
        action="ontology.instances.import",
        target_type="ontology_profile", target_id=reg.profile_id,
        detail=f"{entity_type} 新增 {upserted.created} / 更新 {upserted.updated}",
        client_ip=await get_client_ip(request),
    )
    result["created"] = upserted.created
    result["updated"] = upserted.updated
    return result


@router.post("/instances", response_model=InstanceOut, status_code=201)
async def create_instance(
    payload: InstanceCreate, admin: AdminUser, db: DbSession, request: Request
) -> InstanceOut:
    reg = _registry(payload.profile_id)
    check = validate_entity(reg, payload.entity_type, payload.name, payload.props)
    if not check.ok:
        raise BizError("；".join(check.errors), 400, "ONTOLOGY_INVALID_INSTANCE")

    existing = await inst_mod.get_instance(
        db, reg.profile_id, payload.entity_type, payload.name, payload.kb_id
    )
    if existing is not None:
        raise BizError(
            f"实例已存在：{payload.entity_type}/{payload.name}（kb_id={payload.kb_id}）",
            409,
            "ONTOLOGY_INSTANCE_EXISTS",
        )

    draft = inst_mod.InstanceDraft(
        profile_id=reg.profile_id,
        entity_type=payload.entity_type,
        name=payload.name,
        uid=reg.uid_for(payload.entity_type, payload.name, payload.kb_id),
        kb_id=payload.kb_id,
        props=payload.props,
        ranges=payload.ranges,
        evidence=payload.evidence,
        source=payload.source,
        status=payload.status,
    )
    await inst_mod.upsert_instances(db, [draft])
    await db.commit()
    row = await inst_mod.get_instance(
        db, reg.profile_id, payload.entity_type, payload.name, payload.kb_id
    )
    if row is not None:
        await _sync_graph(nodes=[inst_mod.to_node(row)])
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.instance.create",
        target_type=payload.entity_type,
        target_id=payload.name,
        detail=f"kb_id={payload.kb_id}",
        client_ip=await get_client_ip(request),
    )
    return InstanceOut.model_validate(row)


@router.patch("/instances/{instance_id}", response_model=InstanceOut)
async def patch_instance(
    instance_id: int,
    payload: InstancePatch,
    admin: AdminUser,
    db: DbSession,
    request: Request,
) -> InstanceOut:
    row = await inst_mod.get_instance_by_id(db, instance_id)
    if row is None:
        raise BizError("实例不存在", 404, "ONTOLOGY_INSTANCE_NOT_FOUND")

    reg = _registry(row.profile_id)
    new_props = payload.props if payload.props is not None else dict(row.props or {})
    check = validate_entity(reg, row.entity_type, row.name, new_props)
    if not check.ok:
        raise BizError("；".join(check.errors), 400, "ONTOLOGY_INVALID_INSTANCE")

    changed = inst_mod.apply_changes(
        row,
        props=payload.props,
        ranges=payload.ranges,
        # 证据是**并集**语义：前端重复提交同一份说明（比如只是改了别的字段）不会越堆越多，
        # 也不会覆盖人工写的证据。用户实测过：改两次实例，证据列变成 3 条。
        evidence=(
            inst_mod.dedupe_evidence([*(row.evidence or []), *payload.evidence])
            if payload.evidence is not None
            else None
        ),
        status=payload.status,
        source=payload.source,
    )
    if not changed:
        return InstanceOut.model_validate(row)

    await db.commit()
    # commit 会让 ORM 对象过期；不 refresh 就读 updated_at 会触发 MissingGreenlet
    await db.refresh(row)
    await _sync_graph(nodes=[inst_mod.to_node(row)])
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.instance.update",
        target_type=row.entity_type,
        target_id=row.name,
        detail=f"kb_id={row.kb_id}",
        client_ip=await get_client_ip(request),
    )
    return InstanceOut.model_validate(row)


@router.delete("/instances/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: int, admin: AdminUser, db: DbSession, request: Request
) -> None:
    row = await inst_mod.get_instance_by_id(db, instance_id)
    if row is None:
        raise BizError("实例不存在", 404, "ONTOLOGY_INSTANCE_NOT_FOUND")
    name, entity_type, kb_id, uid = row.name, row.entity_type, row.kb_id, row.uid
    # 级联：挂在它身上的关系一起删，不留悬空边（关系表在 PG，必须显式删）
    dropped = await rel_mod.delete_relations_for_instances(db, [uid])
    await inst_mod.delete_instance(db, row)
    await db.commit()
    await _sync_graph(drop_nodes=[uid])
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.instance.delete",
        target_type=entity_type,
        target_id=name,
        detail=f"kb_id={kb_id}；一并删除 {dropped} 条关系",
        client_ip=await get_client_ip(request),
    )


@router.get("/params", response_model=list[CardOut])
async def get_param_cards(
    _admin: AdminUser,
    db: DbSession,
    profile: str | None = Query(None),
    name: list[str] | None = Query(None, description="参数名，可重复传多个"),
    group: str | None = Query(None),
    kb_id: int = Query(0, ge=0),
    climate: str | None = Query(None, description="流域气候：湿润/半湿润/半干旱/干旱"),
    flood_type: str | None = Query(None, description="洪水类型：单峰/多峰"),
    limit: int = Query(50, ge=1, le=200),
) -> list[CardOut]:
    """参数推荐卡：条件收窄 → 冲突并列 → 无依据不出数。"""
    cards = await queries.param_cards(
        db,
        profile_id=profile,
        names=name,
        group=group,
        kb_id=kb_id,
        climate=climate,
        flood_type=flood_type,
        limit=limit,
    )
    return [CardOut(**card) for card in cards]


async def _run_import(
    db: AsyncSession, *, reg, path: Path, kb_id: int, dry_run: bool
) -> ImportResult:
    result = load_prior_excel(path, reg, profile_id=reg.profile_id, kb_id=kb_id)
    # 证据溯源：能对上文档/切片就带上 doc_id/chunk_id，对不上就明确标未溯源
    result = await resolve_evidence(db, result, kb_id=kb_id)
    preview = [
        {
            "name": d.name,
            "display_zh": d.props.get("display_zh"),
            "group": d.props.get("group"),
            "unit": d.props.get("unit"),
            "range": [d.props.get("range_low"), d.props.get("range_high")],
            "initial": d.props.get("initial"),
            "conditions": len(d.ranges),
            "has_evidence": bool(d.evidence),
            "evidence_level": (
                "traceable" if any(e.get("traceable") for e in d.evidence)
                else "doc_level" if any(e.get("doc_id") for e in d.evidence)
                else "claim" if d.evidence else "none"
            ),
        }
        for d in result.drafts
    ]
    out = ImportResult(
        dry_run=dry_run,
        profile_id=reg.profile_id,
        rows=len(result.rows),
        drafts=len(result.drafts),
        warnings=result.warnings,
        errors=result.errors,
        preview=preview,
    )
    if dry_run or not result.drafts:
        if dry_run and result.drafts:
            # 预演也要给出「会新增/会更新/不会变」——只报 0 的话用户以为导入没生效。
            # 直接跑一次真实写入再回滚，保证预演数字和正式导入**完全一致**
            # （自己算一遍的话，证据合并语义很难对得上，会多报一堆"更新"）。
            try:
                upsert = await inst_mod.upsert_instances(db, result.drafts)
                out.created, out.updated, out.unchanged = (
                    upsert.created,
                    upsert.updated,
                    upsert.unchanged,
                )
            finally:
                await db.rollback()  # 预演：一个字都不落库
        return out
    upsert = await inst_mod.upsert_instances(db, result.drafts)
    await db.commit()
    out.created, out.updated, out.unchanged = upsert.created, upsert.updated, upsert.unchanged
    return out


@router.post("/params/import", response_model=ImportResult)
async def import_params(
    admin: AdminUser,
    db: DbSession,
    request: Request,
    file: UploadFile = File(..., description="参数先验 Excel"),
    profile: str | None = Query(None),
    kb_id: int = Query(0, ge=0),
    dry_run: bool = Query(True, description="默认只预演，false 才写库"),
) -> ImportResult:
    reg = _registry(profile)
    # 参数表只认 Parameter 类型。库里没有这个类型时，
    # 逐行报 13 条「未定义实体类型」既吵又难懂——直接说清楚该切库。
    if reg.entity(_ENTITY_TYPE_DEFAULT) is None:
        raise BizError(
            f"当前图谱库「{reg.library_name}」没有「参数」类型（{_ENTITY_TYPE_DEFAULT}），"
            f"参数表导入只适用于定义了该类型的库（如调参本体）",
            400,
            "ONTOLOGY_IMPORT_NO_PARAM_TYPE",
        )
    suffix = Path(file.filename or "params.xlsx").suffix or ".xlsx"
    if suffix.lower() not in (".xlsx", ".xlsm"):
        raise BizError("只支持 .xlsx 参数表", 400, "ONTOLOGY_IMPORT_BAD_TYPE")

    content = await file.read()
    if not content:
        raise BizError("上传的文件是空的", 400, "ONTOLOGY_IMPORT_EMPTY")
    if len(content) > _MAX_IMPORT_BYTES:
        raise BizError("文件超过 10MB，参数表不该这么大", 413, "ONTOLOGY_IMPORT_TOO_LARGE")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        result = await _run_import(db, reg=reg, path=tmp_path, kb_id=kb_id, dry_run=dry_run)
    except BizError:
        raise
    except Exception as exc:  # noqa: BLE001 - Excel 读坏是常见用户问题
        raise BizError(f"参数表解析失败：{exc}", 400, "ONTOLOGY_IMPORT_FAILED") from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    if not dry_run and result.drafts:
        await audit.record_audit(
            actor_id=admin.id,
            actor_name=admin.username,
            action="ontology.params.import",
            target_type="ontology_instance",
            target_id=reg.profile_id,
            detail=f"新增 {result.created} / 更新 {result.updated} / 无变化 {result.unchanged}",
            client_ip=await get_client_ip(request),
        )
    return result


# ==================== 关系（ABox 的边，真相源在 PG） ====================


async def _instance_type_of(db: AsyncSession, uid: str, role: str, reg):
    """按 uid 取实例，顺带拿到它的实体类型（校验关系端点要用）。"""
    row = await inst_mod.get_instance_by_uid(db, uid)
    if row is None or row.profile_id != reg.profile_id:
        raise BizError(f"{role}不存在：{uid}", 400, "ONTOLOGY_RELATION_ENDPOINT_MISSING")
    return row


@router.get("/relations", response_model=list[RelationOut])
async def list_relations(
    _admin: AdminUser,
    db: DbSession,
    profile: str | None = Query(None),
    subject_uid: str | None = Query(None),
    object_uid: str | None = Query(None),
    touch_uid: str | None = Query(None, description="主语或宾语是它"),
    predicate: str | None = Query(None),
    kb_id: int | None = Query(None),
    status: str | None = Query("active", description="active / archived / all"),
    limit: int = Query(1000, ge=1, le=5000),
) -> list[RelationOut]:
    rows = await rel_mod.list_relations(
        db,
        profile_id=profile or settings.ontology_profile,
        subject_uid=subject_uid,
        object_uid=object_uid,
        touch_uid=touch_uid,
        predicate=predicate,
        kb_id=kb_id,
        status=None if status in (None, "all") else status,
        limit=limit,
    )
    return [RelationOut.model_validate(r) for r in rows]


@router.post("/relations", response_model=RelationOut, status_code=201)
async def create_relation(
    payload: RelationCreate, admin: AdminUser, db: DbSession, request: Request
) -> RelationOut:
    reg = _registry(payload.profile_id)
    subject = await _instance_type_of(db, payload.subject_uid, "主语", reg)
    obj = await _instance_type_of(db, payload.object_uid, "宾语", reg)
    check = validate_relation(reg, payload.predicate, subject.entity_type, obj.entity_type)
    if not check.ok:
        raise BizError("；".join(check.errors), 400, "ONTOLOGY_INVALID_RELATION")

    existing = await rel_mod.get_relation(
        db, reg.profile_id, payload.subject_uid, payload.predicate, payload.object_uid, payload.kb_id
    )
    if existing is not None:
        raise BizError(
            "这条关系已经存在，直接在列表里改它即可", 409, "ONTOLOGY_RELATION_EXISTS"
        )

    row = await rel_mod.upsert_relation(
        db,
        rel_mod.RelationDraft(
            profile_id=reg.profile_id,
            subject_uid=payload.subject_uid,
            predicate=payload.predicate,
            object_uid=payload.object_uid,
            kb_id=payload.kb_id,
            props=payload.props,
            evidence=payload.evidence,
            source=payload.source,
            status=payload.status,
        ),
    )
    await db.commit()
    await db.refresh(row)
    await _sync_graph(edges=[rel_mod.to_edge(row)])
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.relation.create",
        target_type="ontology_relation",
        target_id=str(row.id),
        detail=f"{payload.subject_uid} -{payload.predicate}-> {payload.object_uid}",
        client_ip=await get_client_ip(request),
    )
    return RelationOut.model_validate(row)


@router.patch("/relations/{relation_id}", response_model=RelationOut)
async def patch_relation(
    relation_id: int,
    payload: RelationPatch,
    admin: AdminUser,
    db: DbSession,
    request: Request,
) -> RelationOut:
    row = await rel_mod.get_relation_by_id(db, relation_id)
    if row is None:
        raise BizError("关系不存在", 404, "ONTOLOGY_RELATION_NOT_FOUND")

    reg = _registry(row.profile_id)
    old_predicate = row.predicate
    changed = rel_mod.apply_changes(
        row,
        predicate=payload.predicate,
        props=payload.props,
        evidence=payload.evidence,
        status=payload.status,
        source=payload.source,
    )
    if not changed:
        return RelationOut.model_validate(row)

    # 改了关系类型或状态 → 端点校验要按新值重做
    if row.predicate != old_predicate or payload.status is not None:
        subject = await _instance_type_of(db, row.subject_uid, "主语", reg)
        obj = await _instance_type_of(db, row.object_uid, "宾语", reg)
        check = validate_relation(reg, row.predicate, subject.entity_type, obj.entity_type)
        if not check.ok:
            raise BizError("；".join(check.errors), 400, "ONTOLOGY_INVALID_RELATION")

    await db.commit()
    await db.refresh(row)
    if row.predicate != old_predicate:
        # 关系类型变了：旧边要删掉，否则图里会同时存在新旧两条
        await _sync_graph(drop_edges=[(row.subject_uid, old_predicate, row.object_uid)])
    await _sync_graph(edges=[rel_mod.to_edge(row)])
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.relation.update",
        target_type="ontology_relation",
        target_id=str(row.id),
        detail=f"{row.subject_uid} -{row.predicate}-> {row.object_uid}",
        client_ip=await get_client_ip(request),
    )
    return RelationOut.model_validate(row)


@router.delete("/relations/{relation_id}", status_code=204)
async def delete_relation(
    relation_id: int, admin: AdminUser, db: DbSession, request: Request
) -> None:
    row = await rel_mod.get_relation_by_id(db, relation_id)
    if row is None:
        raise BizError("关系不存在", 404, "ONTOLOGY_RELATION_NOT_FOUND")
    subject_uid, predicate, object_uid = row.subject_uid, row.predicate, row.object_uid
    await rel_mod.delete_relation(db, row)
    await db.commit()
    await _sync_graph(drop_edges=[(subject_uid, predicate, object_uid)])
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.relation.delete",
        target_type="ontology_relation",
        target_id=str(relation_id),
        detail=f"{subject_uid} -{predicate}-> {object_uid}",
        client_ip=await get_client_ip(request),
    )


@router.post("/ask")
async def admin_ask(body: AskIn, _admin: AdminUser, db: DbSession) -> dict:
    """本体问答试验台（管理员用，和对外 /v1/ontology/ask 同一套逻辑）。

    存在的意义：让「本体事实 + 知识库原文」的效果可以在界面上直接验证，
    不用去配 X-API-Key 走命令行。
    """
    pack = await retrieve_with_ontology(
        db,
        body.question,
        kb_id=body.kb_id,
        profile_id=body.profile_id,
        conditions={"climate": body.climate, "flood_type": body.flood_type},
        min_facts=body.min_facts,
    )
    return pack.to_dict()


# ==================== 对外接口（agent 用，X-API-Key） ====================


@public_router.get("/types")
async def public_types(db: DbSession, profile: str | None = Query(None)) -> dict:
    """本体里有哪些实体类型、关系类型、各自属性（agent 用来规划调用）。"""
    return _registry(profile).to_dict()


@public_router.get("/params", response_model=list[CardOut])
async def public_params(
    db: DbSession,
    profile: str | None = Query(None),
    name: list[str] | None = Query(None, description="参数名，可重复传"),
    group: str | None = Query(None),
    kb_id: int = Query(0, ge=0),
    climate: str | None = Query(None),
    flood_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[CardOut]:
    """参数推荐卡：条件收窄 → 冲突并列 → 无依据不出数。"""
    cards = await queries.param_cards(
        db,
        profile_id=profile,
        names=name,
        group=group,
        kb_id=kb_id,
        climate=climate,
        flood_type=flood_type,
        limit=limit,
    )
    return [CardOut(**card) for card in cards]


@public_router.get("/search")
async def public_search(
    db: DbSession,
    q: str = Query(..., min_length=1, max_length=200),
    kb_id: int | None = Query(None),
    profile: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
) -> list[dict]:
    """实体链接：问题里的词 → 本体实例 / 图节点。"""
    return await queries.search_entities(
        db, q, profile_id=profile, kb_id=kb_id, limit=limit
    )


@public_router.get("/entities/{uid}")
async def public_entity(
    uid: str,
    db: DbSession,
    depth: int = Query(1, ge=1, le=2),
    include_evidence: bool = Query(True),
) -> dict:
    """实体详情 + 邻居（本体实例优先，找不到再查图）。"""
    return await queries.entity_detail(
        db, uid, depth=depth, include_evidence=include_evidence
    )


@public_router.get("/neighbors/{uid}")
async def public_neighbors(
    uid: str,
    depth: int = Query(1, ge=1, le=2),
    limit: int = Query(100, ge=1, le=200),
) -> dict:
    """纯图查询：某个节点周围 1~2 跳的子图。"""
    return await queries.graph_neighbors(uid, depth=depth, limit=limit)


@public_router.post("/ask")
@limiter.limit(settings.external_rate_limit)
async def public_ask(request: Request, body: AskIn, db: DbSession) -> dict:
    """本体优先问答：返回事实包；本体事实不够时回退向量检索并标 fallback_used。"""
    pack = await retrieve_with_ontology(
        db,
        body.question,
        kb_id=body.kb_id,
        profile_id=body.profile_id,
        conditions={"climate": body.climate, "flood_type": body.flood_type},
        min_facts=body.min_facts,
    )
    return pack.to_dict()
