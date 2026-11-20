"""按 TBox 约束体检一个本体（只读）。

用法（backend 目录下）：

    python -m scripts.check_ontology --profile reservoir_flood
    python -m scripts.check_ontology --profile reservoir_flood --json
    python -m scripts.check_ontology --profile reservoir_flood --severity error

退出码：0 = 没有 error 级违规；1 = 有 error 级违规；2 = 本体加载失败。

为什么单独做成脚本而不是塞进 seed：约束检查是**日常动作**。
改完数据、换完本体、导入一批实例之后都该跑一次，不该只有"重新灌库"时才跑。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.db.session import async_session_factory  # noqa: E402
from app.services.graph.ontology import constraints as con_mod  # noqa: E402
from app.services.graph.ontology.registry import ProfileError, get_registry  # noqa: E402


def _print_report(registry, violations, summary, severity: str) -> None:
    source = registry.source_path.name if registry.source_path else "-"
    print(f"本体 {registry.profile_id}（{source}）")
    print(f"约束 {len(registry.constraints)} 条，实例与关系已读取，开始求值\n")
    if not violations:
        print("全部通过 ✔")
        return

    shown = [v for v in violations if severity == "all" or v.severity == "error"]
    hidden = len(violations) - len(shown)
    for item in shown:
        print(f"  {item.describe()}")
        detail = item.detail
        if detail.get("actual") is not None and detail.get("expected") is not None:
            print(f"      实际 {detail['actual']}，要求 {detail['expected']}")
    if hidden:
        print(f"\n  …还有 {hidden} 条 warning 未显示（--severity all 可看全）")
    print(
        f"\n合计 {summary['total']} 条：error {summary['errors']}、"
        f"warning {summary['warnings']}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="按 TBox 约束体检本体")
    ap.add_argument("--profile", default="reservoir_flood", help="profile_id 或 YAML 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 CI / 前端用）")
    ap.add_argument(
        "--severity",
        choices=("error", "all"),
        default="error",
        help="打印到哪一级（不影响退出码，退出码只看 error）",
    )
    args = ap.parse_args()

    try:
        registry = get_registry(args.profile)
    except ProfileError as exc:
        print(f"本体加载失败：{exc}", file=sys.stderr)
        return 2

    async with async_session_factory() as db:
        violations = await con_mod.check_database(db, registry)

    summary = con_mod.summarize(violations)
    if args.json:
        print(
            json.dumps(
                {
                    "profile_id": registry.profile_id,
                    "summary": summary,
                    "violations": [v.to_dict() for v in violations],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_report(registry, violations, summary, args.severity)

    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
