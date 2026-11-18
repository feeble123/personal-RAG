"""O8-2 运行记录回流：一次率定 → Run 节点 + 用到的参数 + 达到的指标。

外部建模 agent 跑完一次率定，把结果报过来，本体里就留下一条可追溯的记录：
用了哪些参数、各取什么值、达到什么指标、用的什么策略。

**三条约定**

1. **参数必须已在库里**。参数是人梳理的（有单位、范围、敏感度、依据），
   程序不替人补参数——报上来的名字对不上就明确列出来，不静默丢弃。
2. **指标可以自动建**。一次运行测到的指标名就是事实（不是从文档里挖出来的知识），
   没有就先建一个空的 Metric 节点，之后再补方向/单位。
3. **幂等**。同一 `run_id` 重复上报只更新值，不会堆出第二个 Run；
   重报时先清掉这次运行旧的 USED/ACHIEVED 边，避免旧参数残留成幽灵边。

写入只走 PG（真相源），图由调用方同步——和实例/关系完全同一套分工。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyInstance, OntologyRelation
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology import relations as rel_mod
from app.services.graph.ontology.registry import OntologyRegistry

logger = logging.getLogger(__name__)

REL_USED = "USED"
REL_ACHIEVED = "ACHIEVED"
_REQUIRED_TYPES = ("Run", "Parameter", "Metric")
_REQUIRED_RELATIONS = (REL_USED, REL_ACHIEVED)


@dataclass(frozen=True)
class RunInput:
    """一次运行要报回来的东西。`run_id` 是幂等键，由上报方生成。"""

    run_id: str
    started_at: str | None = None
    objective: str | None = None
    strategy: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    kb_id: int = 0


@dataclass
class RunResult:
    run_uid: str = ""
    created: bool = False
    updated: bool = False
    used: list[dict[str, Any]] = field(default_factory=list)
    achieved: list[dict[str, Any]] = field(default_factory=list)
    missing_parameters: list[str] = field(default_factory=list)
    created_metrics: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_uid": self.run_uid,
            "created": self.created,
            "updated": self.updated,
            "used": self.used,
            "achieved": self.achieved,
            "missing_parameters": self.missing_parameters,
            "created_metrics": self.created_metrics,
            "warnings": self.warnings,
        }


def _require(reg: OntologyRegistry) -> None:
    missing = [t for t in _REQUIRED_TYPES if reg.entity(t) is None]
    if missing:
        raise ValueError(
            f"当前图谱库「{reg.library_name}」缺少类型：{'、'.join(missing)}；"
            "运行记录需要 Run / Parameter / Metric（如「调参本体」）"
        )
    missing_rel = [r for r in _REQUIRED_RELATIONS if reg.relation(r) is None]
    if missing_rel:
        raise ValueError(
            f"当前图谱库「{reg.library_name}」缺少关系：{'、'.join(missing_rel)}"
        )


async def _index_by_name(
    db: AsyncSession, reg: OntologyRegistry, entity_type: str
) -> dict[str, OntologyInstance]:
    """按实例名（标识或中文名）建索引——上报方用哪个名字都能对上。"""
    rows = list(
        (
            await db.scalars(
                select(OntologyInstance).where(
                    OntologyInstance.profile_id == reg.profile_id,
                    OntologyInstance.entity_type == entity_type,
                )
            )
        ).all()
    )
    index: dict[str, OntologyInstance] = {}
    for row in rows:
        index[str(row.name)] = row
        display = str((row.props or {}).get("display_zh") or "").strip()
        if display:
            index.setdefault(display, row)
    return index


def _run_evidence(run_id: str, started_at: str | None) -> list[dict[str, Any]]:
    """把「本次运行」本身作为边的证据：不是文档，如实标不可回溯原文。"""
    note = f"运行记录 {run_id}" + (f"（{started_at}）" if started_at else "")
    return [
        {
            "type": "运行记录",
            "note": note,
            "traceable": False,
            "resolved_by": "none",
        }
    ]


async def record_run(db: AsyncSession, reg: OntologyRegistry, run: RunInput) -> RunResult:
    """写入一次运行记录。调用方负责 commit（以及把结果同步到图库）。"""
    _require(reg)
    run_id = (run.run_id or "").strip()
    if not run_id:
        raise ValueError("run_id 不能为空（它是幂等键）")

    result = RunResult()
    run_uid = reg.uid_for("Run", run_id, run.kb_id)
    result.run_uid = run_uid

    params = await _index_by_name(db, reg, "Parameter")
    metrics = await _index_by_name(db, reg, "Metric")

    # 1) Run 节点（同名 run_id 重复上报 = 更新）
    props = {
        k: v
        for k, v in {
            "started_at": run.started_at,
            "strategy": run.strategy,
            "objective_text": run.objective,
            "note": run.note,
        }.items()
        if v not in (None, "")
    }
    upsert = await inst_mod.upsert_instances(
        db,
        [
            inst_mod.InstanceDraft(
                profile_id=reg.profile_id,
                entity_type="Run",
                name=run_id,
                uid=run_uid,
                kb_id=run.kb_id,
                props=props,
                source="import",
            )
        ],
    )
    result.created = upsert.created > 0
    result.updated = upsert.updated > 0

    # 2) 指标：库里没有就先建（运行测到的指标名是事实）
    new_metrics = [
        inst_mod.InstanceDraft(
            profile_id=reg.profile_id,
            entity_type="Metric",
            name=name,
            uid=reg.uid_for("Metric", name),
            kb_id=0,
            props={"note": f"由运行记录 {run_id} 自动创建，方向/单位待补"},
            source="import",
        )
        for name in run.metrics
        if name not in metrics
    ]
    if new_metrics:
        await inst_mod.upsert_instances(db, new_metrics)
        result.created_metrics = [d.name for d in new_metrics]
        metrics = await _index_by_name(db, reg, "Metric")

    # 3) 重报：先清掉这次运行旧的边，别让上次的参数残留成幽灵边
    await db.execute(
        delete(OntologyRelation).where(
            OntologyRelation.profile_id == reg.profile_id,
            OntologyRelation.subject_uid == run_uid,
            OntologyRelation.predicate.in_(_REQUIRED_RELATIONS),
        )
    )

    evidence = _run_evidence(run_id, run.started_at)
    for name, value in (run.parameters or {}).items():
        row = params.get(str(name))
        if row is None:
            result.missing_parameters.append(str(name))
            continue
        await rel_mod.upsert_relation(
            db,
            rel_mod.RelationDraft(
                profile_id=reg.profile_id,
                subject_uid=run_uid,
                predicate=REL_USED,
                object_uid=row.uid,
                kb_id=run.kb_id,
                props={"value": value},
                evidence=evidence,
                source="import",
            ),
        )
        result.used.append({"parameter": row.name, "value": value})

    for name, value in (run.metrics or {}).items():
        row = metrics.get(str(name))
        if row is None:  # 上面刚建过，理论上到不了这里
            continue
        await rel_mod.upsert_relation(
            db,
            rel_mod.RelationDraft(
                profile_id=reg.profile_id,
                subject_uid=run_uid,
                predicate=REL_ACHIEVED,
                object_uid=row.uid,
                kb_id=run.kb_id,
                props={"value": value},
                evidence=evidence,
                source="import",
            ),
        )
        result.achieved.append({"metric": row.name, "value": value})

    if result.missing_parameters:
        result.warnings.append(
            "这些参数在库里不存在，已跳过（先建参数再上报）："
            + "、".join(result.missing_parameters)
        )
    return result


async def list_runs(
    db: AsyncSession, reg: OntologyRegistry, *, limit: int = 50
) -> list[dict[str, Any]]:
    """按 `started_at` 倒序列出运行记录（给前端表格用）。"""
    rows = list(
        (
            await db.scalars(
                select(OntologyInstance)
                .where(
                    OntologyInstance.profile_id == reg.profile_id,
                    OntologyInstance.entity_type == "Run",
                )
                .limit(limit)
            )
        ).all()
    )
    rows.sort(key=lambda r: str((r.props or {}).get("started_at") or ""), reverse=True)

    uids = [r.uid for r in rows]
    edges: dict[str, list[OntologyRelation]] = {uid: [] for uid in uids}
    if uids:
        for rel in (
            await db.scalars(
                select(OntologyRelation).where(
                    OntologyRelation.subject_uid.in_(uids),
                    OntologyRelation.predicate.in_(_REQUIRED_RELATIONS),
                    OntologyRelation.status == "active",
                )
            )
        ).all():
            edges.setdefault(rel.subject_uid, []).append(rel)

    objects = {}
    object_uids = {r.object_uid for rels in edges.values() for r in rels}
    if object_uids:
        for row in (
            await db.scalars(
                select(OntologyInstance).where(OntologyInstance.uid.in_(object_uids))
            )
        ).all():
            objects[row.uid] = str((row.props or {}).get("display_zh") or row.name)

    out = []
    for row in rows:
        props = row.props or {}
        out.append(
            {
                "uid": row.uid,
                "name": row.name,
                "started_at": props.get("started_at"),
                "strategy": props.get("strategy"),
                "objective": props.get("objective_text"),
                "parameters": [
                    {
                        "name": objects.get(rel.object_uid, rel.object_uid),
                        "value": (rel.props or {}).get("value"),
                    }
                    for rel in edges.get(row.uid, [])
                    if rel.predicate == REL_USED
                ],
                "metrics": [
                    {
                        "name": objects.get(rel.object_uid, rel.object_uid),
                        "value": (rel.props or {}).get("value"),
                    }
                    for rel in edges.get(row.uid, [])
                    if rel.predicate == REL_ACHIEVED
                ],
            }
        )
    return out
