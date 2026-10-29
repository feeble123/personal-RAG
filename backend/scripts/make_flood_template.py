"""生成「防洪链台账模板」Excel（四张表 + 说明 + 填写示例）。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe -X utf8 scripts\\make_flood_template.py
    .venv\\Scripts\\python.exe -X utf8 scripts\\make_flood_template.py --out data/templates/自定义.xlsx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.services.graph.template_io import REQUIRED_SHEETS, write_template  # noqa: E402

DEFAULT_OUT = BASE_DIR / "templates" / "flood_chain_template.xlsx"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成防洪链台账模板")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    args = parser.parse_args()

    out = write_template(Path(args.out))
    print(f"模板已生成：{out}")
    print("工作表：" + "、".join(["说明", *REQUIRED_SHEETS, "填写示例"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
