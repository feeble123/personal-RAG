"""把本体数据从 PostgreSQL 导成 YAML 数据文件（只读，不写库）。

**为什么需要它**：换一座水库时，人不想改 Python；但也不该从零手抄一遍。
先从库里导出成表，改完再导回去——这是「数据外化」的闭环，也是它的验收方式
（导出 → 清空 → 导入 → 数据必须一模一样）。

**只导出人能看懂的东西**：文件、页码、逐字引句。`chunk_id` 是入库产物，
换一次解析器就全变；写进数据文件等于把数据绑死在某一版解析结果上。
导入时会按「文件 + 引句（+ 页码）」重新回找切片。

用法（backend 目录下）：

    python -m scripts.export_ontology_data --profile reservoir_flood
    python -m scripts.export_ontology_data --profile reservoir_flood -o ../data/x.yaml
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
from sqlalchemy import select  # noqa: E402

from app.db.models import OntologyInstance, OntologyRelation  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services.graph.ontology.registry import get_registry  # noqa: E402

HEADER = """\
# ============================================================
# 本体数据（ABox）：实例 + 关系 + 每条出处的逐字引句
# ------------------------------------------------------------
# 这份文件是从库里导出的，也可以直接改完再导回去：
#
#   python -m scripts.import_ontology_data --profile {profile} --data <本文件>
#
# 规矩：
#   · evidence 里的 quote 必须是**原文逐字**，导入时会按「文件 + 引句」回找切片；
#     找不到就报错、不写入——宁可少建一条，也不要建一条点不回原文的"事实"。
#   · page 是**书内印刷页码**（不是 PDF 物理页），只用来在多个候选里缩小范围。
#   · props 里随主语变的值（如时限）要挂在**关系**上，不要挂在实例上。
# ============================================================
"""


def _evidence_out(items: Any) -> list[dict[str, Any]]:
    """库里的证据 → 数据文件里的证据（只留文件、页码、引句）。"""
    out: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        doc = str(raw.get("doc_name") or "").strip()
        quote = str(raw.get("quote") or raw.get("note") or "").strip()
        if not doc or not quote:
            continue  # 没有文件或没有引句的证据在数据文件里没有意义
        item: dict[str, Any] = {"doc": doc, "quote": quote}
        page = raw.get("printed_page")
        if page not in (None, ""):
            item["page"] = page
        out.append(item)
    return out


async def build(profile_id: str) -> dict[str, Any]:
    async with async_session_factory() as db:
        instances = list(
            (
                await db.execute(
                    select(OntologyInstance)
                    .where(
                        OntologyInstance.profile_id == profile_id,
                        OntologyInstance.status == "active",
                    )
                    .order_by(OntologyInstance.entity_type, OntologyInstance.name)
                )
            ).scalars()
        )
        relations = list(
            (
                await db.execute(
                    select(OntologyRelation)
                    .where(
                        OntologyRelation.profile_id == profile_id,
                        OntologyRelation.status == "active",
                    )
                    .order_by(
                        OntologyRelation.subject_uid,
                        OntologyRelation.predicate,
                        OntologyRelation.object_uid,
                    )
                )
            ).scalars()
        )

    where = {row.uid: (row.entity_type, row.name) for row in instances}

    data: dict[str, Any] = {"profile_id": profile_id, "instances": [], "relations": []}
    for row in instances:
        item: dict[str, Any] = {"type": row.entity_type, "name": row.name}
        if row.props:
            item["props"] = dict(row.props)
        evidence = _evidence_out(row.evidence)
        if evidence:
            item["evidence"] = evidence
        data["instances"].append(item)

    for rel in relations:
        subject = where.get(rel.subject_uid)
        obj = where.get(rel.object_uid)
        if subject is None or obj is None:
            # 端点实例不在本 profile 里（历史遗留的孤儿边）：不导出，导回去也建不出来
            print(
                f"跳过孤儿关系：{rel.subject_uid} -{rel.predicate}-> {rel.object_uid}",
                file=sys.stderr,
            )
            continue
        item = {
            "subject": {"type": subject[0], "name": subject[1]},
            "predicate": rel.predicate,
            "object": {"type": obj[0], "name": obj[1]},
        }
        if rel.props:
            item["props"] = dict(rel.props)
        evidence = _evidence_out(rel.evidence)
        if evidence:
            item["evidence"] = evidence
        data["relations"].append(item)

    return data


async def main() -> int:
    ap = argparse.ArgumentParser(description="把本体数据导出成 YAML")
    ap.add_argument("--profile", required=True, help="profile_id（如 reservoir_flood）")
    ap.add_argument("-o", "--out", help="输出路径；不写则打印到标准输出")
    args = ap.parse_args()

    reg = get_registry(args.profile)
    data = await build(reg.profile_id)

    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    text = HEADER.format(profile=reg.profile_id) + body
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(
            f"已导出 {reg.profile_id}：实例 {len(data['instances'])}、"
            f"关系 {len(data['relations'])} → {path}"
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
