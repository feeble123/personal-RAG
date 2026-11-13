"""清掉实例上重复的证据条目（一次性维护脚本）。

**为什么会重复**：编辑实例时，表单把已有的第一条依据预填回来，保存时又原样追加一遍。
接口层已经改成并集语义，前端也不再重复提交；这个脚本负责把**已经坏掉的历史数据**修回来。

判定重复：`(doc_id, chunk_id, type, note)` 四元组相同即视为同一条（与
`instances.merge_evidence` 的去重口径一致）。

用法（backend 目录下）：
    .venv\\Scripts\\python.exe -m scripts.dedupe_evidence           # 只看
    .venv\\Scripts\\python.exe -m scripts.dedupe_evidence --apply   # 真改
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
    parser = argparse.ArgumentParser(description="清理重复证据")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    parser.add_argument("--profile", default=None, help="只处理某个图谱库")
    args = parser.parse_args()

    from sqlalchemy import select

    from app.db.models import OntologyInstance
    from app.db.session import async_session_factory
    from app.services.graph.ontology.instances import dedupe_evidence

    async with async_session_factory() as db:
        stmt = select(OntologyInstance)
        if args.profile:
            stmt = stmt.where(OntologyInstance.profile_id == args.profile)
        rows = (await db.scalars(stmt)).all()

        touched = 0
        removed = 0
        for row in rows:
            evidence = list(row.evidence or [])
            if len(evidence) < 2:
                continue
            deduped = dedupe_evidence(evidence)
            if len(deduped) == len(evidence):
                continue
            touched += 1
            removed += len(evidence) - len(deduped)
            print(f"  {row.profile_id} / {row.name}: {len(evidence)} → {len(deduped)} 条证据")
            if args.apply:
                row.evidence = deduped
        if args.apply and touched:
            await db.commit()
        print("-" * 50)
        print(
            f"涉及 {touched} 个实例，可去掉 {removed} 条重复证据"
            + ("（已写入）" if args.apply and touched else "（未改动，加 --apply 执行）")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
