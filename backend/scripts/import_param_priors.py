"""导入参数先验表（Excel → PostgreSQL → Neo4j）。

默认 **dry-run**：只打印会写什么、有哪些告警，不动数据库。
确认无误后加 `--apply` 才真正写入。

用法（在 backend 目录下）：
    python -m scripts.import_param_priors              # 预演
    python -m scripts.import_param_priors --apply      # 真写
    python -m scripts.import_param_priors --apply --no-graph   # 只写 PG，不更新图库
    python -m scripts.import_param_priors --excel D:/我的参数表.xlsx --apply

设计原则：PG 是真相源，图库只是可重建的索引；导入失败不会留下半截数据。
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
from app.services.graph.ontology.params import (  # noqa: E402
    PriorBuildResult,
    load_prior_excel,
    resolve_evidence,
)
from app.services.graph.ontology.registry import get_registry  # noqa: E402

DEFAULT_EXCEL = BASE_DIR / "templates" / "param_priors_template.xlsx"


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _evidence_label(draft) -> str:
    if not draft.evidence:
        return "无"
    if any(e.get("traceable") for e in draft.evidence):
        return "已溯源"
    if any(e.get("doc_id") for e in draft.evidence):
        return "仅到文档"
    return "仅说明"


def _print_preview(result: PriorBuildResult) -> None:
    print(f"\n将写入 {len(result.drafts)} 条参数：")
    header = (
        f"{'参数名':<10}{'中文名':<18}{'分组':<12}{'单位':<6}"
        f"{'物理范围':<14}{'先验范围':<14}{'条件':<6}{'证据':<10}"
    )
    print(header)
    print("-" * len(header))
    for draft in result.drafts:
        props = draft.props
        physical = "—"
        if props.get("physical_low") is not None or props.get("physical_high") is not None:
            physical = f"{_fmt(props.get('physical_low'))}~{_fmt(props.get('physical_high'))}"
        prior = "—"
        if props.get("range_low") is not None or props.get("range_high") is not None:
            prior = f"{_fmt(props.get('range_low'))}~{_fmt(props.get('range_high'))}"
        print(
            f"{draft.name:<10}{props.get('display_zh', ''):<18}{props.get('group', ''):<12}"
            f"{props.get('unit', ''):<6}{physical:<14}{prior:<14}"
            f"{len(draft.ranges):<6}{_evidence_label(draft):<10}"
        )


def _print_issues(result: PriorBuildResult) -> None:
    if result.errors:
        print(f"\n❌ 有 {len(result.errors)} 行因校验不通过被跳过：")
        for item in result.errors:
            print(f"  - {item}")
    if result.warnings:
        print(f"\n⚠️  {len(result.warnings)} 条告警（不影响导入）：")
        for item in result.warnings:
            print(f"  - {item}")


async def _resolve(result: PriorBuildResult, kb_id: int) -> PriorBuildResult:
    """把依据里的文档名/页码解析成 doc_id / chunk_id（找不到就标未溯源）。"""
    from app.db.session import async_session_factory

    try:
        async with async_session_factory() as session:
            return await resolve_evidence(session, result, kb_id=kb_id)
    except Exception as exc:  # noqa: BLE001 - 数据库不可用不该阻断预演
        print(f"⚠️  证据溯源跳过（{exc.__class__.__name__}: {exc}），全部按「未溯源」处理")
        return result


async def _apply(result: PriorBuildResult, *, sync_graph: bool) -> int:
    from app.db.session import async_session_factory
    from app.services.graph.ontology import instances as inst_mod

    async with async_session_factory() as session:
        upsert = await inst_mod.upsert_instances(session, result.drafts)
        await session.commit()
        rows = await inst_mod.list_instances(
            session, profile_id=result.drafts[0].profile_id if result.drafts else None,
            entity_type="Parameter",
        )

    print(
        f"\n[PG] 新增 {upsert.created} / 更新 {upsert.updated} / 无变化 {upsert.unchanged}"
    )

    if not sync_graph:
        print("[图库] 已跳过（--no-graph）")
        return 0
    if not settings.graph_enabled:
        print("[图库] 未启用（GRAPH_ENABLED=false），跳过")
        return 0

    from app.services.graph.store import get_graph_store

    store = get_graph_store()
    if store is None:
        print("[图库] 未启用，跳过")
        return 0
    try:
        count = await inst_mod.sync_to_graph(rows, store)
        if count:
            print(f"[图库] 已同步 {count} 个节点")
        else:
            print(
                "[图库] ⚠️  写入没成功（图库未启动或超时）。PG 数据已保存，"
                "稍后重跑同一条命令即可补齐图库，不会重复写 PG。"
            )
    finally:
        await store.close()
    return 0


async def _run(args: argparse.Namespace, excel: Path, registry) -> int:
    """整个流程跑在**同一个事件循环**里。

    不能拆成两次 asyncio.run：asyncpg 的连接绑定在创建它的事件循环上，
    第二个循环复用会报 `'NoneType' object has no attribute 'send'`。
    """
    result = load_prior_excel(excel, registry, profile_id=registry.profile_id, kb_id=args.kb_id)
    print(f"读到 {len(result.rows)} 行有效数据")
    result = await _resolve(result, args.kb_id)

    _print_preview(result)
    _print_issues(result)

    if not result.drafts:
        print("\n❌ 没有可写入的数据，检查「参数名」「分组」是否填了")
        return 1
    if not args.apply:
        print("\n（预演模式，未写入数据库。确认无误后加 --apply）")
        return 0
    return await _apply(result, sync_graph=not args.no_graph)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入参数先验表")
    parser.add_argument("--excel", default=str(DEFAULT_EXCEL), help="参数表路径")
    parser.add_argument("--profile", default=settings.ontology_profile, help="本体 profile id")
    parser.add_argument("--kb-id", type=int, default=0, help="0 = 全局共享（参数默认全局）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则只预演")
    parser.add_argument("--no-graph", action="store_true", help="只写 PG，不更新图库")
    args = parser.parse_args()

    excel = Path(args.excel)
    print(f"参数表：{excel}")
    print(f"本体  ：{args.profile}（kb_id={args.kb_id}）")
    if not excel.exists():
        print(f"❌ 文件不存在：{excel}")
        print("   可先用 python -m scripts.make_param_priors_template 生成模板")
        raise SystemExit(1)

    registry = get_registry(args.profile)
    raise SystemExit(asyncio.run(_run(args, excel, registry)))


if __name__ == "__main__":
    main()
