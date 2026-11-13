"""从《水文预报》表5.2 补全参数的「敏感程度」（第二个来源，与手册证据并列）。

背景：手册只给了取值范围和默认值，没给敏感性；教材表 5.2「新安江模型各层次参数表」
（第 171 页）明确标了每个参数是「敏感 / 不敏感」。这是**第二条独立证据**，
所以是并列追加，不覆盖手册那条——重导 Excel 也不会把它冲掉（upsert 里证据取并集）。

用法（在 backend 目录下）：
    python -m scripts.enrich_param_sensitivity            # 预演
    python -m scripts.enrich_param_sensitivity --apply    # 写 PG + Excel（默认同时同步图库）

两个如实交代的地方：
1. 教材只有「敏感 / 不敏感」两级，TBox 的枚举是「高 / 中 / 低」。
   这里做**单调映射** 敏感→高、不敏感→低，并在证据的 `snippet` 里保留原文用词；
   没有把「不敏感」说成「中」——两级来源推不出三级，那是编。
2. 教材表里没有 DM（深层蓄水容量）这一行，所以 wdm 的敏感度**仍然留空**，
   脚本只报告、不猜。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.config import settings  # noqa: E402

TEXTBOOK_LIKE = "%水文预报%"
SENSITIVITY_PAGE = 171  # 表5.2 新安江模型各层次参数表

# 教材参数符号 → 我们的参数名。ke 对应 KC（蒸散发折算），不是教材表里的 KE（马斯京根演算参数）
SYMBOL_TO_PARAM = {
    "KC": "ke", "C": "c", "UM": "wum", "LM": "wlm", "DM": "wdm",
    "IM": "a_imp", "B": "b", "SM": "sm", "EX": "ex",
    "KI": "ki", "KG": "kg", "CI": "ci", "CG": "cg",
}

# 教材两级 → TBox 三级（单调映射，不造「中」）
LEVEL_MAP = {"敏感": "高", "不敏感": "低"}


def parse_table52(texts: list[str]) -> dict[str, str]:
    """表5.2 → {参数符号: 敏感程度}。行格式：层次|层次|符号|意义|敏感程度|取值范围。"""
    out: dict[str, str] = {}
    for text in texts:
        for line in text.splitlines():
            if line.count("|") < 4:
                continue
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 6:
                continue
            symbol, level = cells[2].upper(), cells[4]
            if symbol in SYMBOL_TO_PARAM and level in LEVEL_MAP:
                out.setdefault(symbol, level)
    return out


def _evidence_item(*, symbol: str, level: str, doc, chunk_id: int) -> dict:
    return {
        "type": "教材",
        "note": f"《{Path(doc.filename).stem}》表5.2：{symbol} 敏感程度={level}",
        "doc_id": doc.id,
        "chunk_id": chunk_id,
        "doc_name": doc.filename,
        "page": SENSITIVITY_PAGE,
        "locator": "第171页 表5.2 新安江模型各层次参数表",
        "snippet": f"{symbol} | {level} |",
        "traceable": True,
        "resolved_by": "chunk",
    }


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db.models import Chunk, Document, OntologyInstance
    from app.db.session import async_session_factory
    from app.services.graph.ontology import instances as inst_mod
    from app.services.graph.ontology.validator import validate_entity
    from app.services.graph.ontology.registry import get_registry

    registry = get_registry(args.profile)

    async with async_session_factory() as session:
        doc = (
            await session.execute(
                select(Document).where(Document.filename.like(TEXTBOOK_LIKE)).order_by(Document.id.desc())
            )
        ).scalars().first()
        if doc is None:
            print("❌ 教材还没入库（找不到文件名含「水文预报」的文档）")
            return 1

        rows = (
            await session.execute(
                select(Chunk.id, Chunk.content).where(
                    Chunk.doc_id == doc.id, Chunk.page == SENSITIVITY_PAGE
                )
            )
        ).all()
        if not rows:
            print(f"❌ 教材第 {SENSITIVITY_PAGE} 页没有切片，先确认入库质量")
            return 1

        table = parse_table52([c for _, c in rows])
        # 敏感程度所在的那一条切片（表5.2 可能被切成多条，取第一条含表头的）
        chunk_id = next(
            (cid for cid, text in rows if "敏感程度" in text and "参数符号" in text),
            rows[0][0],
        )
        print(f"教材：{doc.filename}  doc_id={doc.id}  表5.2 在 chunk {chunk_id}")
        print(f"表5.2 给出敏感程度的参数：{len(table)} 个\n")

        instances = (
            await session.execute(
                select(OntologyInstance).where(
                    OntologyInstance.profile_id == registry.profile_id,
                    OntologyInstance.entity_type == "Parameter",
                )
            )
        ).scalars().all()
        by_name = {r.name: r for r in instances}

        header = f"{'参数':8s}{'符号':6s}{'教材原文':10s}{'写入值':8s}{'当前值':8s}{'证据':8s}"
        print(header)
        print("-" * len(header))

        changed, missing, skipped = [], [], []
        for symbol, level in sorted(table.items(), key=lambda kv: SYMBOL_TO_PARAM[kv[0]]):
            name = SYMBOL_TO_PARAM[symbol]
            row = by_name.get(name)
            if row is None:
                skipped.append(name)
                continue
            mapped = LEVEL_MAP[level]
            current = (row.props or {}).get("sensitivity")
            already = any(
                isinstance(e, dict) and e.get("doc_id") == doc.id for e in (row.evidence or [])
            )
            print(
                f"{name:8s}{symbol:6s}{level:10s}{mapped:8s}{str(current or '空'):8s}"
                f"{'已有' if already else '追加'}"
            )
            if current == mapped and already:
                continue
            changed.append((row, mapped, level, symbol))

        # 教材没给的参数，如实报出来
        covered = {SYMBOL_TO_PARAM[s] for s in table}
        missing = [n for n in by_name if n not in covered]
        if skipped:
            print(f"\n⚠️  库里有、教材符号里没有：{', '.join(skipped)}（跳过）")
        if missing:
            print(f"⚠️  教材表里没有这些参数：{', '.join(sorted(missing))}（敏感度保持原样，不猜）")

        if not changed:
            print("\n没有需要更新的参数。")
            return 0
        if not args.apply:
            print(f"\n（预演模式：将更新 {len(changed)} 条。确认无误后加 --apply）")
            return 0

        for row, mapped, level, symbol in changed:
            props = dict(row.props or {})
            props["sensitivity"] = mapped
            draft = inst_mod.InstanceDraft(
                profile_id=row.profile_id,
                entity_type=row.entity_type,
                name=row.name,
                uid=row.uid,
                kb_id=row.kb_id,
                props=props,
                ranges=list(row.ranges or []),
                evidence=list(row.evidence or []),
                source=row.source,
                status=row.status,
            )
            # 证据在此处并集（手册那条保留，教材这条追加）
            draft = replace(
                draft,
                evidence=inst_mod.merge_evidence(
                    row.evidence,
                    [_evidence_item(symbol=symbol, level=level, doc=doc, chunk_id=chunk_id)],
                ),
            )
            check = validate_entity(registry, draft.entity_type, draft.name, draft.props)
            if not check.ok:
                print(f"❌ {row.name}: {'；'.join(check.errors)}")
                continue
            await inst_mod.upsert_instances(session, [draft])

        await session.commit()
        print(f"\n[PG] 已更新 {len(changed)} 条参数（证据追加，原手册证据保留）")

    if args.excel:
        _update_excel(changed, args)

    if not args.no_graph and settings.graph_enabled:
        from app.services.graph.store import get_graph_store

        store = get_graph_store()
        if store is not None:
            try:
                async with async_session_factory() as session:
                    rows = await inst_mod.list_instances(
                        session, profile_id=registry.profile_id, entity_type="Parameter"
                    )
                count = await inst_mod.sync_to_graph(rows, store)
                print(f"[图库] 已同步 {count} 个节点" if count else "[图库] ⚠️ 写入没成功，稍后重跑补齐")
            finally:
                await store.close()
    return 0


def _update_excel(changed, args: argparse.Namespace) -> None:
    """把敏感度同步写回人工维护的 Excel（否则下次重导会以表里为准）。"""
    from openpyxl import load_workbook

    from app.services.graph.ontology.params import COLUMNS  # 列名 → 字段名

    path = Path(args.excel)
    if not path.exists():
        print(f"[Excel] 跳过：{path} 不存在")
        return
    sensitivity_header = next(
        (title for title, key in COLUMNS if key == "sensitivity"), "敏感度"
    )
    wb = load_workbook(path)
    ws = wb.active
    headers = [str(c.value).strip() if c.value else "" for c in ws[1]]
    try:
        name_col = headers.index("参数名") + 1
        sens_col = headers.index(sensitivity_header) + 1
    except ValueError:
        print(f"[Excel] 跳过：表头里没有「参数名」或「{sensitivity_header}」")
        return

    by_name = {row.name: mapped for row, mapped, _, _ in changed}
    touched = 0
    for r in range(2, ws.max_row + 1):
        name = str(ws.cell(r, name_col).value or "").strip()
        if name in by_name:
            ws.cell(r, sens_col).value = by_name[name]
            touched += 1
    wb.save(path)
    print(f"[Excel] 已同步 {touched} 行敏感度 → {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="用教材表5.2 补全参数敏感程度")
    parser.add_argument("--profile", default=settings.ontology_profile)
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则只预演")
    parser.add_argument("--no-graph", action="store_true", help="只写 PG，不更新图库")
    parser.add_argument(
        "--excel",
        default=str(BASE_DIR / "templates" / "param_priors_template.xlsx"),
        help="同时把敏感度写回这张参数表；传空字符串可跳过",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
