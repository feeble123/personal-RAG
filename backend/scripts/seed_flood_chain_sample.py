"""给「防洪链台账」填一套**样例数据**，用来跑通入库→图谱→查询的完整链路。

⚠️ 这些是虚构的示例工程与乡镇，只为验证功能。正式使用时：
     python -m scripts.seed_flood_chain_sample --clear   # 清空四张表的样例数据
    再按真实《防洪抢险应急预案》《汛期调度规程》重填。

注意：备注里刻意不写「示例行」三个字——导入器会跳过含「示例行」的行，
这里要的是能真正导入的样例数据。

用法（在 backend 目录下）：
    python -m scripts.seed_flood_chain_sample            # 写入样例数据
    python -m scripts.seed_flood_chain_sample --clear    # 清空（只留表头）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from openpyxl import load_workbook  # noqa: E402

DEFAULT_EXCEL = BASE_DIR / "templates" / "flood_chain_template.xlsx"
MARK = "样例数据（待替换真实台账）"

PROJECTS = [
    ["青溪水库", "reservoir", "", "青溪县水务局", "杨柳镇", 62.0, 1580, MARK],
    ["青溪水库溢洪道", "spillway", "青溪水库", "", "", "", "", MARK],
    ["白河水库", "reservoir", "", "白河县水利局", "石桥镇", 45.5, 860, MARK],
]

CONDITIONS = [
    ["青溪设计工况", "design", "100年一遇", 418.20, 1350, "青溪水库汛期调度规程 4.2", "设计洪水", MARK],
    ["青溪校核工况", "check", "1000年一遇", 419.60, 1820, "青溪水库汛期调度规程 4.3", "", MARK],
    ["白河设计工况", "design", "50年一遇", 312.40, 760, "白河水库汛期调度规程 3.1", "", MARK],
]

REACHES = [
    ["青溪水库下游河段1", "青溪水库", "坝下", "杨柳大桥", 5.4, 900, "防洪抢险应急预案 附表3", MARK],
    ["青溪水库下游河段2", "青溪水库", "杨柳大桥", "两河汇口", 7.8, 650, "防洪抢险应急预案 附表3", MARK],
    ["白河水库下游河段1", "白河水库", "坝下", "石桥", 4.2, 520, "防洪抢险应急预案 附表2", MARK],
]

IMPACTS = [
    ["青溪设计工况", "青溪水库下游河段1", "杨柳镇", "河湾村", "是", "900 m³/s", "是",
     "杨柳镇中心小学", "沿河路→镇道→中心小学", "应急预案 5.3", "杨柳镇防汛责任人", MARK],
    ["青溪设计工况", "青溪水库下游河段1", "杨柳镇", "上坝村", "是", "900 m³/s", "否",
     "", "", "应急预案 5.3", "", MARK],
    ["青溪校核工况", "青溪水库下游河段2", "石桥镇", "下河村", "是", "650 m³/s", "是",
     "石桥镇初级中学", "河堤路→镇道→初级中学", "应急预案 5.4", "石桥镇防汛责任人", MARK],
    ["白河设计工况", "白河水库下游河段1", "石桥镇", "河口村", "是", "520 m³/s", "否",
     "", "", "应急预案 4.2", "", MARK],
]

SHEETS = {
    "工程表": PROJECTS,
    "工况表": CONDITIONS,
    "河段表": REACHES,
    "影响对象表": IMPACTS,
}


def _clear(ws) -> None:
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="防洪链台账样例数据")
    parser.add_argument("--excel", default=str(DEFAULT_EXCEL), help="台账文件路径")
    parser.add_argument("--clear", action="store_true", help="清空数据（只留表头）")
    args = parser.parse_args()

    path = Path(args.excel)
    if not path.exists():
        print(f"❌ 找不到台账：{path}")
        print("   先运行 python -m scripts.make_flood_template 生成模板")
        raise SystemExit(1)

    wb = load_workbook(path)
    total = 0
    for sheet_name, rows in SHEETS.items():
        if sheet_name not in wb.sheetnames:
            print(f"⚠️  缺少工作表「{sheet_name}」，跳过")
            continue
        ws = wb[sheet_name]
        _clear(ws)
        if not args.clear:
            for row in rows:
                ws.append(row)
            total += len(rows)
        print(f"{sheet_name}: {'已清空' if args.clear else f'写入 {len(rows)} 行'}")

    wb.save(path)
    if args.clear:
        print(f"已清空样例数据 → {path}")
    else:
        print(f"已写入 {total} 行样例数据 → {path}")
        print("提示：这些是虚构数据，仅供功能验证；正式使用前请用 --clear 清掉再填真实台账。")


if __name__ == "__main__":
    main()
