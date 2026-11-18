"""给证据补上「逐字引用」（一次性维护脚本）。

**问题**：早期导入的证据只有 `snippet`，而且是**拼接出来的摘要**——
案例：教材表 5.2 的 `CG | 敏感 |`（跳过了中间那列参数名），
原文其实是 `第四层次 | 汇流计算 | CG | 地下水消退系数 | 敏感 |`。
这样的摘录和原文对不上，点开原文时没法高亮，用户以为"这段不在原文里"。

**做法**：拿摘录里的每个 token 去片段里找**同一行**，找到就把整行当成 `quote` 存下来。
只在能逐字对上的时候写，对不上就跳过（不编）。

用法（backend 目录下）：
    .venv\\Scripts\\python.exe -m scripts.fix_evidence_quotes            # 只看
    .venv\\Scripts\\python.exe -m scripts.fix_evidence_quotes --apply    # 真改
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR).strip() and str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

_MAX_QUOTE = 200


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _find_quote(content: str, snippet: str) -> str | None:
    """在片段里找包含摘录所有 token 的那一行，作为逐字引用。"""
    tokens = [t.strip() for t in re.split(r"[|｜]", snippet or "") if t.strip()]
    if not tokens:
        return None
    for line in (content or "").splitlines():
        text = line.strip()
        if len(text) < 4:
            continue
        if all(token in text for token in tokens):
            return text[:_MAX_QUOTE]
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description="给证据补逐字引用")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    args = parser.parse_args()

    from sqlalchemy import select

    from app.db.models import Chunk, OntologyInstance
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        rows = list((await db.scalars(select(OntologyInstance))).all())
        touched = 0
        skipped = 0
        for row in rows:
            items = list(row.evidence or [])
            if not items:
                continue
            changed = False
            new_items = []
            for item in items:
                if not isinstance(item, dict) or item.get("quote"):
                    new_items.append(item)
                    continue
                chunk_id = item.get("chunk_id")
                snippet = str(item.get("snippet") or "")
                if chunk_id is None or not snippet:
                    new_items.append(item)
                    continue
                chunk = await db.get(Chunk, int(chunk_id))
                if chunk is None:
                    new_items.append(item)
                    continue
                # 已经能逐字对上就不用改
                if _normalize(snippet) in _normalize(chunk.content):
                    new_items.append(item)
                    continue
                quote = _find_quote(chunk.content or "", snippet)
                if quote is None:
                    skipped += 1
                    print(f"  跳过 {row.profile_id}/{row.name}：摘录在片段里对不上（{snippet[:30]}）")
                    new_items.append(item)
                    continue
                new_items.append({**item, "quote": quote})
                changed = True
                print(f"  {row.profile_id}/{row.name}: 补引用「{quote[:50]}」")
            if changed:
                touched += 1
                if args.apply:
                    row.evidence = new_items
        if args.apply and touched:
            await db.commit()
        print("-" * 60)
        print(
            f"涉及 {touched} 个实例；对不上而跳过 {skipped} 条"
            + ("（已写入）" if args.apply and touched else "（未改动，加 --apply 执行）")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
