"""用**模型手册原文**填参数先验表（每一行都能点回手册里的那一行）。

数据来源（唯一）：《新安江模型手册.pdf》第 4 页「4. 模型参数」表，字段为
    序号 | 分类 | 符号 | 参数意义 | 取值范围 | 默认值

本脚本把「取值范围」写进通用下限/上限，「默认值」写进初值，并把
「依据文档 / 依据位置 / 原文摘录」填好，导入时会定位到具体切片。

**不填**：物理上下限（手册没给物理边界）、条件范围（手册没按流域分）、敏感度。
手册里没有的，一律留空，不拿别的来源冒充。

用法（在 backend 目录下）：
    python -m scripts.seed_param_priors_from_manual
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from openpyxl import load_workbook  # noqa: E402

DEFAULT_EXCEL = BASE_DIR / "templates" / "param_priors_template.xlsx"

DOC_NAME = "新安江模型手册.pdf"
TYPE = "手册"

# 参数名 → (下限, 上限, 默认值, 页码, 手册里的参数名)
# 数值逐条抄自手册第 4/5 页的「取值范围」「默认值」两列。
VALUES: dict[str, tuple[float, float, float, int, str]] = {
    "ke": (0.50, 2.00, 1.00, 4, "蒸散发能力折算系数"),
    "wum": (5, 30, 10, 4, "上层张力水蓄水容量"),
    "wlm": (40, 90, 70, 4, "下层张力水蓄水容量"),
    "wdm": (35, 80, 50, 4, "深层张力水蓄水容量"),
    "c": (0.09, 0.20, 0.16, 4, "深层蒸散发系数"),
    "a_imp": (0.005, 0.50, 0.01, 4, "不透水面积占全流域"),
    "b": (0.1, 0.4, 0.2, 4, "指数型张力水蓄水容"),
    "sm": (5, 50, 10, 4, "自由水蓄水容量"),
    "ex": (1.0, 2.0, 1.5, 4, "指数型自由水蓄水容"),
    "ki": (0.10, 0.50, 0.35, 5, "壤中流出流系数"),
    "kg": (0.10, 0.50, 0.35, 5, "地下水出流系数"),
    "ci": (0.010, 0.999, 0.800, 5, "壤中流消退系数"),
    "cg": (0.050, 0.999, 0.950, 5, "地下径流流消退系数"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="用模型手册填参数先验表")
    parser.add_argument("--out", default=str(DEFAULT_EXCEL))
    args = parser.parse_args()
    path = Path(args.out)
    if not path.exists():
        print(f"❌ 找不到参数表：{path}")
        print("   先运行 python -m scripts.make_param_priors_template")
        raise SystemExit(1)

    wb = load_workbook(path)
    ws = wb[wb.sheetnames[0]]
    header = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    col = {name: idx + 1 for idx, name in enumerate(header) if name}
    missing = [h for h in ("依据文档", "依据位置", "原文摘录") if h not in col]
    if missing:
        print(f"❌ 表头缺少列：{missing}，请重新生成模板")
        raise SystemExit(1)

    filled = 0
    for row in range(2, ws.max_row + 1):
        name = ws.cell(row=row, column=col["参数名"]).value
        if not name:
            continue
        name = str(name).strip()
        if name not in VALUES:
            continue
        low, high, default, page, manual_name = VALUES[name]

        ws.cell(row=row, column=col["物理下限"], value=None)
        ws.cell(row=row, column=col["物理上限"], value=None)
        ws.cell(row=row, column=col["通用下限"], value=low)
        ws.cell(row=row, column=col["通用上限"], value=high)
        ws.cell(row=row, column=col["初值"], value=default)
        ws.cell(row=row, column=col["条件范围"], value=None)
        ws.cell(row=row, column=col["敏感度"], value=None)
        ws.cell(row=row, column=col["依据类型"], value=TYPE)
        ws.cell(
            row=row,
            column=col["依据"],
            value=f"《新安江模型手册》参数表：取值范围 [{low},{high}]，默认值 {default}",
        )
        ws.cell(row=row, column=col["依据文档"], value=DOC_NAME)
        ws.cell(row=row, column=col["依据位置"], value=f"第{page}页 4.模型参数")
        ws.cell(row=row, column=col["原文摘录"], value=manual_name)
        ws.cell(
            row=row,
            column=col["备注"],
            value="数值取自模型手册；物理边界与条件范围手册未给出，留空",
        )
        filled += 1

    wb.save(path)
    print(f"已用手册数据填写 {filled} 个参数 → {path}")


if __name__ == "__main__":
    main()
