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

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import AdminUser, DbSession, get_client_ip, verify_external_api_key
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.modules.ontology.schemas import (
    AskIn,
    CardOut,
    ImportResult,
    InstanceCreate,
    InstanceOut,
    InstancePatch,
    ReloadOut,
)
from app.services import audit
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology.cards import recommend
from app.services.graph.ontology.linking import link_graph_entities, link_instances
from app.services.graph.ontology.params import load_prior_excel, resolve_evidence
from app.services.graph.ontology.registry import get_registry
from app.services.graph.ontology.retrieval import retrieve_with_ontology
from app.services.graph.ontology.validator import validate_entity

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
    wanted = (profile or settings.ontology_profile or "").strip()
    if not wanted:
        raise BizError("未配置本体 profile（ONTOLOGY_PROFILE）", 400, "ONTOLOGY_PROFILE_MISSING")
    try:
        return get_registry(wanted)
    except Exception as exc:  # noqa: BLE001 - profile 写错是用户可修复的问题
        raise BizError(f"本体 profile 加载失败：{exc}", 400, "ONTOLOGY_PROFILE_INVALID") from exc


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
        evidence=payload.evidence,
        status=payload.status,
        source=payload.source,
    )
    if not changed:
        return InstanceOut.model_validate(row)

    await db.commit()
    # commit 会让 ORM 对象过期；不 refresh 就读 updated_at 会触发 MissingGreenlet
    await db.refresh(row)
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
    name, entity_type, kb_id = row.name, row.entity_type, row.kb_id
    await inst_mod.delete_instance(db, row)
    await db.commit()
    await audit.record_audit(
        actor_id=admin.id,
        actor_name=admin.username,
        action="ontology.instance.delete",
        target_type=entity_type,
        target_id=name,
        detail=f"kb_id={kb_id}",
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
    reg = _registry(profile)
    conditions = {"climate": climate, "flood_type": flood_type}
    cards = await recommend(
        db,
        profile_id=reg.profile_id,
        names=name,
        conditions=conditions,
        kb_id=kb_id,
        limit=limit,
    )
    if group:
        cards = [c for c in cards if c.group == group]
    return [CardOut(**card.to_dict()) for card in cards]


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
    reg = _registry(profile)
    cards = await recommend(
        db,
        profile_id=reg.profile_id,
        names=name,
        conditions={"climate": climate, "flood_type": flood_type},
        kb_id=kb_id,
        limit=limit,
    )
    if group:
        cards = [c for c in cards if c.group == group]
    return [CardOut(**card.to_dict()) for card in cards]


@public_router.get("/search")
async def public_search(
    db: DbSession,
    q: str = Query(..., min_length=1, max_length=200),
    kb_id: int | None = Query(None),
    profile: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
) -> list[dict]:
    """实体链接：问题里的词 → 本体实例 / 图节点。"""
    reg = _registry(profile)
    rows = await inst_mod.list_instances(
        db, profile_id=reg.profile_id, status="active", limit=500
    )
    hits = link_instances(rows, q)
    hits += await link_graph_entities(db, q, kb_id=kb_id, limit=limit)
    hits.sort(key=lambda x: -x.score)
    return [h.to_dict() for h in hits[:limit]]


@public_router.get("/entities/{uid}")
async def public_entity(
    uid: str,
    db: DbSession,
    depth: int = Query(1, ge=1, le=2),
    include_evidence: bool = Query(True),
) -> dict:
    """实体详情 + 邻居（本体实例优先，找不到再查图）。"""
    from app.services.graph.store import get_graph_store

    row = await inst_mod.get_instance_by_uid(db, uid)
    store = get_graph_store()
    node = await store.get_node(uid) if store is not None else None
    if row is None and node is None:
        raise BizError("实体不存在", 404, "ONTOLOGY_ENTITY_NOT_FOUND")

    detail: dict = {
        "uid": uid,
        "instance": InstanceOut.model_validate(row).model_dump() if row else None,
        "node": {"name": node.name, "entity_type": node.type_name, "props": node.props}
        if node
        else None,
    }
    if include_evidence and row is not None:
        detail["evidence"] = row.evidence or []
    if store is not None:
        slice_ = await store.neighbors(uid, depth=depth)
        names = {n.uid: n.name for n in slice_.nodes}
        detail["neighbors"] = [
            {
                "uid": n.uid,
                "name": n.name,
                "entity_type": n.type_name,
            }
            for n in slice_.nodes
        ]
        detail["edges"] = [
            {
                "subject": names.get(e.subject_uid, e.subject_uid),
                "predicate": e.predicate_name,
                "object": names.get(e.object_uid, e.object_uid),
                "evidence_text": e.evidence_text,
                "chunk_ids": e.chunk_ids,
            }
            for e in slice_.edges
        ]
    return detail


@public_router.get("/neighbors/{uid}")
async def public_neighbors(
    uid: str,
    depth: int = Query(1, ge=1, le=2),
    limit: int = Query(100, ge=1, le=200),
) -> dict:
    """纯图查询：某个节点周围 1~2 跳的子图。"""
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        raise BizError("图库未启用（GRAPH_ENABLED=false）", 503, "GRAPH_DISABLED")
    slice_ = await store.neighbors(uid, depth=depth, limit=limit)
    names = {n.uid: n.name for n in slice_.nodes}
    return {
        "uid": uid,
        "nodes": [
            {"uid": n.uid, "name": n.name, "entity_type": n.type_name, "props": n.props}
            for n in slice_.nodes
        ],
        "edges": [
            {
                "subject": names.get(e.subject_uid, e.subject_uid),
                "predicate": e.predicate_name,
                "object": names.get(e.object_uid, e.object_uid),
                "status": e.status,
                "confidence": e.confidence,
                "evidence_text": e.evidence_text,
                "chunk_ids": e.chunk_ids,
            }
            for e in slice_.edges
        ],
        "truncated": slice_.truncated,
    }


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
