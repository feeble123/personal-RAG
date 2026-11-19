"""把 PG 里的本体实例与关系重放进图库（迁移 / 灾难恢复 / 换图库时用）。

**这不是启动路径。** 正常运行时的同步是在写实例/关系的那一刻完成的（`_sync_graph`），
图库只是派生索引；本脚本用于三种情况：

1. 之前图库连不上，写进去的东西只在 PG、没同步到图库（本次就是这种）；
2. 换了图库实现（memory ↔ neo4j）或图库数据被清空，需要重建索引；
3. 从备份恢复了 PG，需要把图重新长出来。

**幂等**：Neo4j 侧按 uid / 三元组 MERGE，重复跑不会产生重复节点或边。
**只重放 active**：status 不是 active 的实例和关系不进去（图库不该有"已删除"的边）。

用法（backend 目录下）：

    python -m scripts.replay_graph_from_pg                 # 直接重放全部
    python -m scripts.replay_graph_from_pg --dry-run       # 只统计，不写图
    python -m scripts.replay_graph_from_pg --profile flood # 只重放某个本体

退出码：0 = 全部写入成功；1 = 有实例或边没写进去（图库可能不可用）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select  # noqa: E402

from app.db.models import OntologyInstance, OntologyRelation  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services.graph.ontology import instances as inst_mod  # noqa: E402
from app.services.graph.ontology import relations as rel_mod  # noqa: E402
from app.services.graph.store import get_graph_store  # noqa: E402


async def _load(profile: str | None) -> tuple[list, list]:
    async with async_session_factory() as db:
        inst_stmt = select(OntologyInstance).where(OntologyInstance.status == "active")
        rel_stmt = select(OntologyRelation).where(OntologyRelation.status == "active")
        if profile:
            inst_stmt = inst_stmt.where(OntologyInstance.profile_id == profile)
            rel_stmt = rel_stmt.where(OntologyRelation.profile_id == profile)
        rows = (await db.execute(inst_stmt)).scalars().all()
        rels = (await db.execute(rel_stmt)).scalars().all()
    return list(rows), list(rels)


def _by_profile(rows: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.profile_id] = counts.get(row.profile_id, 0) + 1
    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description="从 PostgreSQL 重放本体图到图库")
    parser.add_argument("--profile", help="只重放指定 profile_id（缺省全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写图库")
    args = parser.parse_args()

    store = get_graph_store()
    if store is None:
        print("图库未启用（GRAPH_ENABLED=false），没有可重放的目标", file=sys.stderr)
        return 1
    print(f"图库实现：{type(store).__name__}")

    instances, relations = await _load(args.profile)
    print(f"PG 中 active 实例 {len(instances)} 个、关系 {len(relations)} 条")
    for profile_id, n in sorted(_by_profile(instances).items()):
        print(f"  · {profile_id}: {n} 个实例")
    if args.dry_run:
        print("--dry-run：不写图库")
        return 0

    written_nodes = await inst_mod.sync_to_graph(instances, store)
    written_edges = await rel_mod.sync_to_graph(relations, store)
    print(f"已写入节点 {written_nodes}/{len(instances)}，边 {written_edges}/{len(relations)}")

    # 抽样验证只在 sync_to_graph 内部做了一次；这里再要一次全库统计，
    # 让"到底进没进去"有第二个人看见。
    stats = await store.stats()
    print(
        "图库当前统计：节点 "
        f"{(stats.get('nodes') or {}).get('total', 0)}，边 {(stats.get('edges') or {}).get('total', 0)}"
    )

    ok = written_nodes == len(instances) and written_edges == len(relations)
    print("完成 ✔" if ok else "有内容没写进去 ✘（图库可能不可用，看上面的日志）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
