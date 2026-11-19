"""清理指向已删除实例的孤儿别名与悬空关系。

为什么需要：早先删实例只删了 `ontology_instances` 一行，别名表与关系表还留着
引用。后果不是报错，而是**静默失效**——问题里说到"青溪水库"，实体链接认得出，
图上一查节点不存在，链路上就少一环，答案看起来"就是不完整"。

用法（backend 目录下）：
    .venv\Scripts\python.exe -m scripts.cleanup_orphan_graph            # 只看
    .venv\Scripts\python.exe -m scripts.cleanup_orphan_graph --apply    # 真删
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


async def main() -> int:
    parser = argparse.ArgumentParser(description="清理孤儿别名与悬空关系")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只统计）")
    args = parser.parse_args()

    from sqlalchemy import delete, or_, select, text

    from app.db.models import GraphEntityAlias, OntologyInstance, OntologyRelation
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        known = set((await db.scalars(select(OntologyInstance.uid))).all())
        alias_uids = list((await db.scalars(select(GraphEntityAlias.node_uid).distinct())).all())
        bad_aliases = [uid for uid in alias_uids if uid not in known]

        relations = (
            await db.execute(select(OntologyRelation.id, OntologyRelation.subject_uid, OntologyRelation.object_uid))
        ).all()
        bad_relations = [r[0] for r in relations if r[1] not in known or r[2] not in known]

        print(f"实例 {len(known)} 个；孤儿别名 {len(bad_aliases)} 个 uid；悬空关系 {len(bad_relations)} 条")
        if bad_aliases:
            print("  别名样例:", bad_aliases[:5])
        if args.apply and (bad_aliases or bad_relations):
            if bad_aliases:
                await db.execute(delete(GraphEntityAlias).where(GraphEntityAlias.node_uid.in_(bad_aliases)))
            if bad_relations:
                await db.execute(delete(OntologyRelation).where(OntologyRelation.id.in_(bad_relations)))
            await db.commit()
            print("  已删除")
        elif not args.apply:
            print("  （未改动，加 --apply 执行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
