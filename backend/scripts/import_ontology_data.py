"""把 YAML 数据文件导进本体库（实例 + 关系 + 证据）。

**这是「换一座水库只换数据文件」的入口。** 数据文件长什么样见
`scripts/export_ontology_data.py`（导出 → 改 → 导回，闭环）。

四条硬规矩：

1. **写之前先整批校验**：类型/属性/端点/枚举对不上就整批拒绝，一条都不写。
   半批写进去最坏——库里既有脏数据又有缺口，还分不清是表错了还是引擎错了。
2. **证据对不上就不写**：`quote` 必须能在指定文档里逐字（忽略空白）找到。
   找不到就报错并跳过该条，**绝不硬指一个切片**。
3. **先预演**：`--dry-run` 只校验、只统计，不落库。批量写数据必须能先看一眼。
4. **写完立刻体检**：跑 TBox 约束，有 error 就返回 1（`--allow-incomplete` 可跳过）。

用法（backend 目录下）：

    python -m scripts.import_ontology_data --profile reservoir_flood \\
        --data app/services/graph/ontology/profiles/reservoir_flood.data.yaml --dry-run

    # 确认无误后去掉 --dry-run；配 --reset 可让库与文件严格一致
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import yaml  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.db.models import Document, OntologyInstance, OntologyRelation  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services.graph.ontology import constraints as con_mod  # noqa: E402
from app.services.graph.ontology import instances as inst_mod  # noqa: E402
from app.services.graph.ontology import relations as rel_mod  # noqa: E402
from app.services.graph.ontology.evidence import load_index, resolve  # noqa: E402
from app.services.graph.ontology.registry import ProfileError, get_registry  # noqa: E402
from app.services.graph.ontology.validator import validate_entity, validate_relation  # noqa: E402
from app.services.graph.store import get_graph_store  # noqa: E402


def _endpoint(raw: Any) -> tuple[str, str] | None:
    """`{type, name}` → (type, name)；写法不对返回 None。"""
    if not isinstance(raw, dict):
        return None
    etype = str(raw.get("type") or "").strip()
    name = str(raw.get("name") or "").strip()
    return (etype, name) if etype and name else None


def _evidence_of(item: dict[str, Any], where: str, errors: list[str]) -> list[dict[str, Any]]:
    """数据文件里的 `{doc, quote, page}` → `evidence.resolve` 能吃的形状。"""
    out: list[dict[str, Any]] = []
    for idx, ev in enumerate(item.get("evidence") or []):
        if not isinstance(ev, dict):
            errors.append(f"{where}.evidence[{idx}] 必须是映射（doc / quote / page）")
            continue
        doc = str(ev.get("doc") or "").strip()
        quote = str(ev.get("quote") or "").strip()
        if not doc or not quote:
            errors.append(f"{where}.evidence[{idx}] 缺 doc 或 quote")
            continue
        page = ev.get("page")
        out.append({"doc": doc, "quote": quote, "page": int(page) if page is not None else None})
    return out


def validate_document(
    reg: Any, raw: dict[str, Any]
) -> tuple[list[str], list[str], dict[tuple[str, str], dict[str, Any]]]:
    """整批校验数据文件。返回 (错误, 告警, 实例索引)。

    **先全部校验、再统一决定写不写**：半批写进去最坏——库里既有脏数据又有缺口，
    还分不清是表错了还是引擎错了。

    这一层是"关系端点"唯一真正被检查的地方：早期的种子脚本直接写库、从不校验，
    于是 `condition --PERFORMED_BY--> 单位`（工况不会"执行"任何动作）这类错误
    在图上躺了很久，直到第一次跑校验才暴露。
    """
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}

    for idx, item in enumerate(raw.get("instances") or []):
        where = f"instances[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{where} 必须是映射")
            continue
        etype = str(item.get("type") or "").strip()
        name = str(item.get("name") or "").strip()
        if not etype or not name:
            errors.append(f"{where} 缺 type 或 name")
            continue
        key = (etype, name)
        if key in seen:
            errors.append(f"{where} 实例重复：{etype}/{name}")
            continue
        seen[key] = item
        result = validate_entity(reg, etype, name, item.get("props") or {})
        errors.extend(result.errors)
        warnings.extend(result.warnings)

    for idx, item in enumerate(raw.get("relations") or []):
        where = f"relations[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{where} 必须是映射")
            continue
        subject = _endpoint(item.get("subject"))
        obj = _endpoint(item.get("object"))
        predicate = str(item.get("predicate") or "").strip()
        if subject is None or obj is None or not predicate:
            errors.append(f"{where} 缺 subject / predicate / object")
            continue
        if subject not in seen:
            errors.append(f"{where} 主语实例不在本文件里：{subject[0]}/{subject[1]}")
        if obj not in seen:
            errors.append(f"{where} 宾语实例不在本文件里：{obj[0]}/{obj[1]}")
        errors.extend(validate_relation(reg, predicate, subject[0], obj[0]).errors)

    return errors, warnings, seen


async def _infer_kb_id(db, instances: list[dict[str, Any]]) -> int | None:
    """从第一条能在库里对上的文档名反查知识库。"""
    names = [
        str(ev.get("doc") or "").strip()
        for item in instances
        for ev in (item.get("evidence") or [])
        if isinstance(ev, dict)
    ]
    for name in names:
        if not name:
            continue
        kb_id = (
            await db.execute(select(Document.kb_id).where(Document.filename == name).limit(1))
        ).scalar()
        if kb_id is not None:
            return int(kb_id)
    return None


async def _reset(db, profile_id: str) -> int:
    """清空该 profile 的实例与关系（含图库节点）。

    **不能写 `async with db.begin()`**：调用到这里时，前面的「读知识库」「建切片索引」
    已经在同一个 Session 上开了事务，再 begin 一次会直接抛
    `A transaction is already begun on this Session`（实测）。
    这里沿用当前事务、直接提交即可。
    """
    uids = [
        row[0]
        for row in (
            await db.execute(
                select(OntologyInstance.uid).where(OntologyInstance.profile_id == profile_id)
            )
        ).all()
    ]
    await db.execute(delete(OntologyRelation).where(OntologyRelation.profile_id == profile_id))
    await db.execute(delete(OntologyInstance).where(OntologyInstance.profile_id == profile_id))
    await db.commit()
    store = get_graph_store()
    if store is not None and uids:
        try:
            await store.delete_nodes(uids)
        except Exception as exc:  # noqa: BLE001
            print(f"图库清理失败（PG 已清空，重放可修正）：{exc}", file=sys.stderr)
    return len(uids)


async def main() -> int:
    ap = argparse.ArgumentParser(description="从 YAML 导入本体实例与关系")
    ap.add_argument("--profile", required=True, help="profile_id（如 reservoir_flood）")
    ap.add_argument("--data", required=True, help="数据文件路径（.yaml）")
    ap.add_argument("--kb", help="知识库名称；不填则按依据里的文档名反查")
    ap.add_argument("--dry-run", action="store_true", help="只校验与统计，不写库")
    ap.add_argument("--reset", action="store_true", help="先清空该本体再写（库与文件严格一致）")
    ap.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="写完即使约束报 error 也返回 0",
    )
    args = ap.parse_args()

    try:
        reg = get_registry(args.profile)
    except ProfileError as exc:
        print(f"本体加载失败：{exc}", file=sys.stderr)
        return 2

    path = Path(args.data)
    if not path.exists():
        print(f"数据文件不存在：{path}", file=sys.stderr)
        return 2
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    file_profile = str(raw.get("profile_id") or "").strip()
    if file_profile and file_profile != reg.profile_id:
        print(
            f"数据文件属于 {file_profile!r}，但导的是 {reg.profile_id!r}——拒绝导入",
            file=sys.stderr,
        )
        return 2

    instances_raw = raw.get("instances") or []
    relations_raw = raw.get("relations") or []

    # ---------- 1. 整批校验（对不上就一条都不写） ----------
    errors, warnings, seen = validate_document(reg, raw)

    if errors:
        print(f"有 {len(errors)} 处校验不通过（一条都没写）：", file=sys.stderr)
        for item in errors[:40]:
            print("  -", item, file=sys.stderr)
        if len(errors) > 40:
            print(f"  …还有 {len(errors) - 40} 条", file=sys.stderr)
        return 1

    # ---------- 2. 证据回找 ----------
    summary: dict[str, int] = {"total": 0, "errors": 0, "warnings": 0}
    async with async_session_factory() as db:
        if args.kb:
            kb_id = (
                await db.execute(select(Document.kb_id).where(Document.filename == args.kb).limit(1))
            ).scalar()
            if kb_id is None:
                print(f"知识库里找不到文档 {args.kb}，无法确定 kb_id", file=sys.stderr)
                return 2
            kb_id = int(kb_id)
        else:
            kb_id = await _infer_kb_id(db, instances_raw)
            if kb_id is None:
                print("按依据里的文档名反查不到知识库；请用 --kb 指定", file=sys.stderr)
                return 2

        index = await load_index(db)
        evidence_errors: list[str] = []
        used = 0

        inst_drafts: list[inst_mod.InstanceDraft] = []
        uids: dict[tuple[str, str], str] = {}
        for (etype, name), item in seen.items():
            evidence = []
            for ev in _evidence_of(item, f"{etype}/{name}", evidence_errors):
                hit = resolve(index, ev["doc"], ev["quote"], ev["page"])
                if hit is None:
                    evidence_errors.append(
                        f"实例「{name}」出处找不到：{ev['doc']} ← {ev['quote'][:40]}"
                    )
                else:
                    evidence.append(hit)
            used += len(evidence)
            uid = reg.uid_for(etype, name, kb_id=kb_id)
            uids[(etype, name)] = uid
            inst_drafts.append(
                inst_mod.InstanceDraft(
                    profile_id=reg.profile_id,
                    entity_type=etype,
                    name=name,
                    uid=uid,
                    kb_id=kb_id,
                    props=dict(item.get("props") or {}),
                    evidence=evidence,
                    source="import",
                )
            )

        rel_drafts: list[rel_mod.RelationDraft] = []
        seen_rel: set[tuple[str, str, str]] = set()
        for idx, item in enumerate(relations_raw):
            subject = _endpoint(item["subject"])
            obj = _endpoint(item["object"])
            predicate = str(item["predicate"]).strip()
            su, ou = uids[subject], uids[obj]
            if (su, predicate, ou) in seen_rel:
                warnings.append(f"relations[{idx}] 与前面的三元组重复，已跳过")
                continue
            seen_rel.add((su, predicate, ou))
            evidence = []
            for ev in _evidence_of(item, f"relations[{idx}]", evidence_errors):
                hit = resolve(index, ev["doc"], ev["quote"], ev["page"])
                if hit is None:
                    evidence_errors.append(
                        f"关系「{subject[1]}-{predicate}->{obj[1]}」出处找不到："
                        f"{ev['doc']} ← {ev['quote'][:40]}"
                    )
                else:
                    evidence.append(hit)
            used += len(evidence)
            rel_drafts.append(
                rel_mod.RelationDraft(
                    profile_id=reg.profile_id,
                    subject_uid=su,
                    predicate=predicate,
                    object_uid=ou,
                    kb_id=kb_id,
                    props=dict(item.get("props") or {}),
                    evidence=evidence,
                    source="import",
                )
            )

        print(
            f"知识库 kb_id={kb_id}｜实例 {len(inst_drafts)} 个、关系 {len(rel_drafts)} 条、"
            f"证据条目 {used} 条"
        )
        if warnings:
            print(f"告警 {len(warnings)} 条（不拦导入）：")
            for item in warnings[:10]:
                print("  -", item)
        if evidence_errors:
            print(f"\n有 {len(evidence_errors)} 处出处对不上（未写入）：", file=sys.stderr)
            for item in evidence_errors[:40]:
                print("  -", item, file=sys.stderr)
            return 1

        if args.dry_run:
            print("--dry-run：校验与出处全部通过，未写库")
            return 0

        if args.reset:
            removed = await _reset(db, reg.profile_id)
            print(f"已清空 profile={reg.profile_id}：实例 {removed} 个")

        result = await inst_mod.upsert_instances(db, inst_drafts)
        for draft in rel_drafts:
            await rel_mod.upsert_relation(db, draft)
        await db.commit()

        rows = (
            await db.execute(
                select(inst_mod.OntologyInstance).where(
                    inst_mod.OntologyInstance.profile_id == reg.profile_id
                )
            )
        ).scalars().all()
        store = get_graph_store()
        nodes_written = await inst_mod.sync_to_graph(rows, store) if store else 0
        rel_rows = (
            await db.execute(
                select(rel_mod.OntologyRelation).where(
                    rel_mod.OntologyRelation.profile_id == reg.profile_id
                )
            )
        ).scalars().all()
        edges_written = await rel_mod.sync_to_graph(rel_rows, store) if store else 0

        print(
            f"实例：新建 {result.created}／更新 {result.updated}／不变 {result.unchanged}；"
            f"关系写入 {len(rel_drafts)} 条"
        )
        print(f"图库同步：节点 {nodes_written}/{len(rows)}，边 {edges_written}/{len(rel_rows)}")

        violations = await con_mod.check_database(db, reg)
        summary = con_mod.summarize(violations)
        if not violations:
            print("\n约束体检：全部通过 ✔")
        else:
            print(
                f"\n约束体检：{summary['total']} 条违规"
                f"（error {summary['errors']}、warning {summary['warnings']}）"
            )
            for item in violations:
                print(f"  {item.describe()}")

    if summary["errors"] and not args.allow_incomplete:
        print(
            f"\n有 {summary['errors']} 条 error 级约束未通过——数据已写入，但本体还不完整。"
            "\n确认这是有意的，可加 --allow-incomplete 跳过该检查。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
