"""Neo4j 图存储冒烟测试（O2 验收用）。

用途：装好 Neo4j Desktop、启动数据库后，跑一遍这条命令就能确认
「连得上 → schema 建好了 → 写得进 → 查得出 → 删得掉」。

用法（在 backend 目录下）：
    python -m scripts.ontology_smoke
    python -m scripts.ontology_smoke --uri bolt://localhost:7687 --user neo4j --password 你的密码
    python -m scripts.ontology_smoke --keep      # 保留测试数据，方便在 Neo4j Browser 里看图

密码优先从 `backend/.env` 的 GRAPH_NEO4J_PASSWORD 读；**不要**把密码写进命令行历史或文档。

测试数据用专属 kb_id=9999，跑完自动删除，不影响真实知识库。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.config import settings  # noqa: E402
from app.services.graph.neo4j_store import Neo4jGraphStore  # noqa: E402
from app.services.graph.schema import Edge, Node  # noqa: E402

SMOKE_KB_ID = 9999


def _node(entity_type: str, name: str, **props) -> Node:
    return Node(
        uid=f"smoke:{entity_type}:{name}",
        entity_type=entity_type,
        name=name,
        props=props,
        kb_ids=[SMOKE_KB_ID],
    )


def _line(title: str) -> None:
    print(f"\n=== {title} ===")


async def run(args: argparse.Namespace) -> int:
    password = args.password or settings.graph_neo4j_password
    store = Neo4jGraphStore(
        uri=args.uri,
        user=args.user,
        password=password,
        database=args.database,
    )

    print(f"目标：{args.uri}  数据库：{args.database}  用户：{args.user}")
    if not password:
        print("❌ 没有密码：请在 backend/.env 配置 GRAPH_NEO4J_PASSWORD，或用 --password 传一次")
        return 2

    _line("1. 健康检查")
    alive = await store.health()
    print("✅ 连得上" if alive else "❌ 连不上（Neo4j 是否已启动？密码是否正确？）")
    if not alive:
        await store.close()
        return 1

    _line("2. 初始化 schema（约束/索引/全文索引）")
    ok = await store.ensure_schema()
    print("✅ schema 就绪" if ok else "❌ schema 初始化失败（看上面日志）")
    if not ok:
        await store.close()
        return 1

    _line("3. 写入两个节点 + 一条关系")
    await store.delete_kb(SMOKE_KB_ID)  # 先清掉上次残留
    param = _node("Parameter", "sm", group="水源划分", unit="mm", physical_low=5, physical_high=45)
    metric = _node("Metric", "NSE", unit="-")
    edge = Edge(
        subject_uid=param.uid,
        predicate="AFFECTS",
        object_uid=metric.uid,
        kb_id=SMOKE_KB_ID,
        chunk_ids=[1],
        evidence_text="冒烟测试数据，非真实依据",
        extractor="manual",
        status="confirmed",
    )
    await store.upsert([param, metric], [edge])
    print(f"写入：{param.name} --AFFECTS--> {metric.name}")

    _line("4. 读回来")
    got = await store.get_node(param.uid)
    print(f"get_node：{'✅ ' + got.name if got else '❌ 没读到'}")

    slice_ = await store.neighbors(param.uid)
    print(f"neighbors：{len(slice_.nodes)} 个节点 / {len(slice_.edges)} 条关系")
    for e in slice_.edges:
        print(f"  {e.subject_uid} -[{e.predicate_name}]-> {e.object_uid}  证据片段={e.chunk_ids}")

    hits = await store.search("sm", kb_id=SMOKE_KB_ID)
    print(f"search('sm')：{'✅ 命中 ' + str(len(hits)) + ' 个' if hits else '❌ 没命中'}")

    stats = await store.stats(kb_id=SMOKE_KB_ID)
    print(f"stats：节点 {stats['nodes']['total']} / 关系 {stats['edges']['total']}")
    print(f"  按类型：{stats['nodes']['by_type']}")
    print(f"  按关系：{stats['edges']['by_type']}")

    if args.keep:
        print(f"\nℹ️  已保留测试数据（kb_id={SMOKE_KB_ID}），可在 Neo4j Browser 执行：")
        print("   MATCH (n:OntologyEntity)-[r:ONT_REL]->(m) RETURN n,r,m LIMIT 25")
    else:
        _line("5. 清理测试数据")
        await store.delete_kb(SMOKE_KB_ID)
        left = await store.stats(kb_id=SMOKE_KB_ID)
        print("✅ 已清空" if left["nodes"]["total"] == 0 else f"⚠️ 仍有残留：{left}")

    await store.close()

    passed = bool(got) and bool(slice_.edges) and bool(hits)
    print("\n" + ("✅ 冒烟测试通过" if passed else "❌ 冒烟测试未通过，见上面明细"))
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Neo4j 图存储冒烟测试")
    parser.add_argument("--uri", default=settings.graph_neo4j_uri, help="bolt 地址")
    parser.add_argument("--user", default=settings.graph_neo4j_user, help="用户名")
    parser.add_argument("--password", default="", help="密码（不填则读 .env）")
    parser.add_argument("--database", default=settings.graph_neo4j_database, help="数据库名")
    parser.add_argument("--keep", action="store_true", help="保留测试数据（方便看图）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
