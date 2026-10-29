"""把「防洪链台账」Excel 导入知识图谱（默认只预览，--apply 才写）。

用法（在 backend 目录）：
    # 1) 先生成模板（若还没有）
    .venv\\Scripts\\python.exe -X utf8 scripts\\make_flood_template.py

    # 2) 预览（不改任何数据）
    .venv\\Scripts\\python.exe -X utf8 scripts\\import_flood_chain.py \\
        --file templates/flood_chain_template.xlsx --kb-name 鲤鱼塘水库

    # 3) 确认无误后写库
    ... --kb-id 3 --apply

前置：.env 里 GRAPH_ENABLED=true、GRAPH_BACKEND=memory（U1 阶段；Neo4j 在后续单元）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import delete, select

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.config import settings  # noqa: E402
from app.core.time import utcnow  # noqa: E402
from app.db.models import (  # noqa: E402
    GraphEdgeEvidence,
    GraphEntityAlias,
    GraphSyncRun,
    KnowledgeBase,
)
from app.db.session import async_session_factory  # noqa: E402
from app.services.graph.flood_chain import build_flood_chain, summarize  # noqa: E402
from app.services.graph.schema import (  # noqa: E402
    GLOBAL_ENTITY_TYPES,
    ExtractionResult,
    normalize_name,
)
from app.services.graph.store import get_graph_store  # noqa: E402
from app.services.graph.template_io import read_template  # noqa: E402


async def resolve_kb(kb_id: int | None, kb_name: str | None) -> tuple[int, str]:
    async with async_session_factory() as db:
        if kb_id is not None:
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is None:
                raise SystemExit(f"知识库 #{kb_id} 不存在")
            return kb.id, kb.name
        if not kb_name:
            raise SystemExit("必须指定 --kb-id 或 --kb-name")
        rows = (await db.execute(select(KnowledgeBase))).scalars().all()
        exact = [kb for kb in rows if kb.name.strip() == kb_name.strip()]
        if not exact:
            fuzzy = [kb for kb in rows if kb_name.strip() in kb.name]
            if len(fuzzy) != 1:
                names = "\n  ".join(f"#{kb.id} {kb.name}" for kb in rows)
                raise SystemExit(f"找不到知识库「{kb_name}」，现有：\n  {names}")
            exact = fuzzy
        return exact[0].id, exact[0].name


def print_report(result: ExtractionResult, kb_label: str, apply: bool) -> None:
    summary = summarize(result)
    mode = "执行模式（--apply）" if apply else "预览模式（不改数据）"
    print("=" * 74)
    print(f"防洪链台账导入 · {mode}")
    print(f"目标知识库：{kb_label}")
    print("=" * 74)
    print(f"节点 {summary['nodes']} 个，关系 {summary['edges']} 条")
    if summary["by_entity_type"]:
        print("\n按实体类型：")
        for key, count in sorted(summary["by_entity_type"].items(), key=lambda x: -x[1]):
            print(f"  {key:<22} {count}")
    if summary["by_relation_type"]:
        print("\n按关系类型：")
        for key, count in sorted(summary["by_relation_type"].items(), key=lambda x: -x[1]):
            print(f"  {key:<22} {count}")

    print("\n抽样（前 8 条关系）：")
    for edge in result.edges[:8]:
        print(f"  {edge.subject_uid} -[{edge.predicate.value}]-> {edge.object_uid}   （{edge.evidence_text}）")

    if summary["warnings"]:
        print(f"\n告警 {len(summary['warnings'])} 条：")
        for warning in summary["warnings"][:20]:
            print(f"  ⚠ {warning}")
        if len(summary["warnings"]) > 20:
            print(f"  …… 另有 {len(summary['warnings']) - 20} 条")
    else:
        print("\n无告警。")


async def persist(kb_id: int, result: ExtractionResult) -> None:
    """写 PG（运行记录 / 证据 / 别名）+ 写图存储。"""
    store = get_graph_store()
    if store is None:
        raise SystemExit(
            "图谱未启用：请在 backend/.env 设置 GRAPH_ENABLED=true、GRAPH_BACKEND=memory 后重试"
        )

    async with async_session_factory() as db:
        run = GraphSyncRun(kb_id=kb_id, mode="import", status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # 幂等：先清掉本库上一次的人工台账证据（文档抽取的证据不受影响）
        await db.execute(
            delete(GraphEdgeEvidence).where(
                GraphEdgeEvidence.kb_id == kb_id,
                GraphEdgeEvidence.extractor == "manual",
            )
        )

        evidence_count = 0
        for edge in result.edges:
            db.add(
                GraphEdgeEvidence(
                    edge_key=edge.key,
                    kb_id=kb_id,
                    doc_id=None,
                    version_id=None,
                    chunk_id=None,
                    doc_name="人工台账",
                    section="防洪链台账",
                    clause_no=None,
                    snippet=edge.evidence_text or "人工台账",
                    extractor="manual",
                    rule_id=None,
                    confidence=1.0,
                    status="confirmed",
                    run_id=run.id,
                )
            )
            evidence_count += 1

        # 别名：全局实体写 kb_id=0，库内实体写真实 kb_id；已存在则跳过
        alias_count = 0
        for node in result.nodes:
            scope = 0 if node.entity_type in GLOBAL_ENTITY_TYPES else kb_id
            for alias in dict.fromkeys([node.name, *node.aliases]):
                normalized = normalize_name(alias)
                if not normalized:
                    continue
                exists = await db.scalar(
                    select(GraphEntityAlias.id).where(
                        GraphEntityAlias.node_uid == node.uid,
                        GraphEntityAlias.kb_id == scope,
                        GraphEntityAlias.normalized_alias == normalized,
                    )
                )
                if exists:
                    continue
                db.add(
                    GraphEntityAlias(
                        node_uid=node.uid,
                        entity_type=node.entity_type.value,
                        name=node.name,
                        alias=alias,
                        normalized_alias=normalized,
                        kb_id=scope,
                    )
                )
                alias_count += 1

        run.entity_count = len(result.nodes)
        run.relation_count = len(result.edges)
        run.status = "succeeded"
        run.warnings = result.warnings or None
        run.finished_at = utcnow()
        await db.commit()

    await store.upsert(result.nodes, result.edges)

    print(f"\n已写入：运行 #{run.id}，节点 {len(result.nodes)}、关系 {len(result.edges)}、"
          f"证据 {evidence_count}、别名 {alias_count}")


async def main_async(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")

    kb_id, kb_name = await resolve_kb(args.kb_id, args.kb_name)
    try:
        sheets = read_template(path)
    except ValueError as exc:
        raise SystemExit(f"{exc}（请用 make_flood_template.py 生成的模板）") from exc
    for key, rows in sheets.items():
        print(f"读取 {key}: {len(rows)} 行")

    result = build_flood_chain(
        kb_id=kb_id,
        projects=sheets["projects"],
        conditions=sheets["conditions"],
        reaches=sheets["reaches"],
        impacts=sheets["impacts"],
    )
    print_report(result, f"#{kb_id} {kb_name}", args.apply)

    if not args.apply:
        print("\n这是预览，没有写入任何数据。确认后加 --apply 执行。")
        return 0
    if not result.nodes:
        raise SystemExit("没有可导入的节点（模板是否为空？示例行不会被导入）")

    await persist(kb_id, result)
    print("\n导入完成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="防洪链台账导入知识图谱")
    parser.add_argument("--file", default="templates/flood_chain_template.xlsx")
    parser.add_argument("--kb-id", type=int, default=None)
    parser.add_argument("--kb-name", default=None)
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    args = parser.parse_args()

    if settings.graph_enabled is False and args.apply:
        print("提示：GRAPH_ENABLED=false，--apply 会被拒绝。先改 backend/.env 再试。")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
