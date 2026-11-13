"""把存量的关系名统一成大写蛇形（`downstream_of` → `DOWNSTREAM_OF`）。

**为什么要迁**：内置防洪链关系用枚举小写值，而 TBox 规范要求大写蛇形。
两者指同一个概念，存储层现在已统一按大写写入，但老数据还是小写——
不迁的话按关系名过滤/分组会各算各的，界面上新建的关系和老边对不上。

**为什么不直接在读的时候兼容**：`relation_label_zh` 确实两边都能显示成中文，
但「同一条关系两种写法」会一直传染到过滤、去重、统计里，越晚迁越贵。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe -m scripts.migrate_relation_names          # 只统计
    .venv\\Scripts\\python.exe -m scripts.migrate_relation_names --apply  # 真正修改
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _canonical(value: str) -> str:
    from app.services.graph.schema import canonical_relation

    return canonical_relation(value)


async def _migrate_graph(apply: bool) -> int:
    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        print("图库未启用，跳过图侧迁移")
        return 0
    if not hasattr(store, "_run"):  # memory 后端没有持久化，无需迁移
        print("当前是内存图存储，无需迁移")
        return 0
    # 图库挂了时 _run 会静默返回空结果，不能把「连不上」报成「没有要迁的」
    if not await store.health():
        print("图库连不上（Neo4j 没启动？），跳过图侧迁移")
        return -1

    rows = await store._run(  # noqa: SLF001 - 一次性维护脚本，直接跑 Cypher 最省事
        "MATCH ()-[r:ONT_REL]->() "
        "WHERE r.relation_type <> toUpper(r.relation_type) "
        "RETURN r.relation_type AS rel, count(*) AS c",
        {},
    )
    if not rows:
        print("图侧：没有需要迁移的关系")
        return 0
    total = 0
    for row in rows:
        name, count = str(row.get("rel")), int(row.get("c") or 0)
        total += count
        print(f"  图侧  {name} → {_canonical(name)}   ({count} 条)")
    if not apply:
        print(f"图侧：共 {total} 条待迁移（未改动，加 --apply 执行）")
        return total

    await store._run(  # noqa: SLF001
        "MATCH ()-[r:ONT_REL]->() "
        "WHERE r.relation_type <> toUpper(r.relation_type) "
        "SET r.relation_type = toUpper(r.relation_type)",
        {},
    )
    print(f"图侧：已迁移 {total} 条")
    return total


async def _migrate_pg(apply: bool) -> int:
    from sqlalchemy import text

    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT predicate, count(*) FROM ontology_relations "
                    "WHERE predicate <> upper(predicate) GROUP BY predicate"
                )
            )
        ).all()
        if not rows:
            print("PG 侧：没有需要迁移的关系")
            return 0
        total = 0
        for predicate, count in rows:
            total += int(count)
            print(f"  PG    {predicate} → {_canonical(predicate)}   ({count} 条)")
        if not apply:
            print(f"PG 侧：共 {total} 条待迁移（未改动，加 --apply 执行）")
            return total

        await db.execute(
            text(
                "UPDATE ontology_relations SET predicate = upper(predicate) "
                "WHERE predicate <> upper(predicate)"
            )
        )
        await db.commit()
        print(f"PG 侧：已迁移 {total} 条")
        return total


async def main() -> int:
    parser = argparse.ArgumentParser(description="关系名大小写统一")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    args = parser.parse_args()

    print("=" * 60)
    print("关系名迁移：小写内置枚举 → 大写蛇形")
    print("=" * 60)
    graph = await _migrate_graph(args.apply)
    pg = await _migrate_pg(args.apply)
    print("-" * 60)
    if args.apply:
        print(f"完成：图侧 {graph} 条 / PG 侧 {pg} 条")
    else:
        print(f"预览：图侧 {graph} 条 / PG 侧 {pg} 条待迁移")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
