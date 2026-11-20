"""7-C C-4 提案落地：把待审的「新实例 / 新关系」真正写进库。

三条规矩：

1. **提案可以有缺口，落地不行**——提案阶段缺必填只提醒（人会补），这里缺就拒，
   并说清缺什么。同一份「哪些字段必填」的知识，两处用的是 `validator.missing_required`。
2. **先实例后关系**——关系两端必须先存在，否则这条边一定悬空。调用方（审核接口）
   负责按依赖排序；这里只负责"两端不存在就如实拒掉"。
3. **只补不覆盖**——实例已经存在时（人工先建了、或上一批提案建过），提案的字段值
   只填进**空字段**，已有值一个字不动。证据是并集。理由和 P2 锚定一样：人工整理
   的值比机器猜的可信。

**失败不吞**：落不下去的提案留在待审区、不标 accepted，并带回人话原因——把
"点了接受但什么也没发生"改成"点了接受，告诉你是谁没建、为什么没建"。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyProposal
from app.services.graph.ontology import instances as inst_mod
from app.services.graph.ontology import relations as rel_mod
from app.services.graph.ontology import validator
from app.services.graph.ontology.registry import OntologyRegistry


@dataclass
class LandingResult:
    """一条提案落地的结果。`subject` 和 `reason` 都是给人看的。"""

    ok: bool
    subject: str = ""
    reason: str = ""
    kind: str = ""
    #: 落地后的行（实例 / 关系）；审核接口拿它去同步图库
    row: object | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "subject": self.subject, "reason": self.reason}


def _payload(proposal: OntologyProposal) -> dict:
    return dict(proposal.payload or {})


def _evidence_of(proposal: OntologyProposal) -> list[dict]:
    return [proposal.evidence] if proposal.evidence else []


async def accept_instance(
    db: AsyncSession, registry: OntologyRegistry, proposal: OntologyProposal
) -> LandingResult:
    """接受一条「新实例」提案。"""
    payload = _payload(proposal)
    entity_type = str(payload.get("entity_type") or proposal.entity_type or "")
    name = str(payload.get("name") or "")
    uid = str(payload.get("uid") or proposal.instance_uid or "")
    props = payload.get("props") if isinstance(payload.get("props"), dict) else {}
    kb_id = int(payload.get("kb_id") or 0)
    subject = name or uid

    if not uid or not name:
        return LandingResult(False, subject, "提案里缺少名称或 uid，没法建实例", "instance")

    existing = await inst_mod.get_instance_by_uid(db, uid)
    if existing is not None:
        # 已经有了：只补空字段 + 并证据，绝不覆盖人工值
        merged = dict(existing.props or {})
        for key, value in props.items():
            if merged.get(key) in (None, ""):
                merged[key] = value
        inst_mod.apply_changes(
            existing,
            props=merged,
            evidence=inst_mod.merge_evidence(existing.evidence or [], _evidence_of(proposal)),
        )
        await db.flush()
        return LandingResult(
            True, subject, "实例已存在，本次只补了空字段与证据", "instance", existing
        )

    check = validator.validate_entity(registry, entity_type, name, props)
    if check.errors:
        return LandingResult(False, subject, "；".join(check.errors[:2]), "instance")

    await inst_mod.upsert_instances(
        db,
        [
            inst_mod.InstanceDraft(
                profile_id=registry.profile_id,
                entity_type=entity_type,
                name=name,
                uid=uid,
                kb_id=kb_id,
                props=dict(props),
                evidence=_evidence_of(proposal),
                source="extracted",
            )
        ],
    )
    created = await inst_mod.get_instance_by_uid(db, uid)
    return LandingResult(True, subject, "", "instance", created)


async def accept_relation(
    db: AsyncSession, registry: OntologyRegistry, proposal: OntologyProposal
) -> LandingResult:
    """接受一条「新关系」提案。两端实例必须已经存在。"""
    payload = _payload(proposal)
    subject_uid = str(payload.get("subject_uid") or proposal.instance_uid or "")
    predicate = str(payload.get("predicate") or "")
    object_uid = str(payload.get("object_uid") or "")
    props = payload.get("props") if isinstance(payload.get("props"), dict) else {}
    kb_id = int(payload.get("kb_id") or 0)
    label = f"{subject_uid} —{predicate}→ {object_uid}"

    if not (subject_uid and predicate and object_uid):
        return LandingResult(False, label, "提案里缺少主语/关系/宾语", "relation")

    subject = await inst_mod.get_instance_by_uid(db, subject_uid)
    obj = await inst_mod.get_instance_by_uid(db, object_uid)
    missing = []
    if subject is None:
        missing.append(f"主语 {subject_uid}")
    if obj is None:
        missing.append(f"宾语 {object_uid}")
    if missing:
        # 最常见的场景：同批的新实例还没被接受。故意不自动建——建什么由人决定
        return LandingResult(
            False,
            label,
            "；".join(missing) + " 还没建，先把它的「新实例」提案接受了",
            "relation",
        )

    check = validator.validate_relation(
        registry, predicate, subject.entity_type, obj.entity_type
    )
    if check.errors:
        return LandingResult(False, label, "；".join(check.errors[:2]), "relation")

    row = await rel_mod.upsert_relation(
        db,
        rel_mod.RelationDraft(
            profile_id=registry.profile_id,
            subject_uid=subject_uid,
            predicate=predicate,
            object_uid=object_uid,
            kb_id=kb_id,
            props=dict(props),
            evidence=_evidence_of(proposal),
            source="extracted",
        ),
    )
    return LandingResult(True, label, "", "relation", row)


async def accept(
    db: AsyncSession, registry: OntologyRegistry, proposal: OntologyProposal
) -> LandingResult:
    """按 kind 分派。不是图提案（attr / anchor）返回 ok=False 交给上游处理。"""
    if proposal.kind == "instance":
        return await accept_instance(db, registry, proposal)
    if proposal.kind == "relation":
        return await accept_relation(db, registry, proposal)
    return LandingResult(False, "", "不是图提案", proposal.kind or "attr")
