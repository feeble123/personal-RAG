"""把 Excel 读成前端能直接渲染的网格。

为什么要后端读：xlsx 是二进制包，前端要渲染就得引一个 ~900KB 的解析库；
而 `openpyxl` 后端本来就有（参数表导入在用），读一次几毫秒。
docx 走的是另一条路（前端 `docx-preview` 排版，见前端 OfficeView）。

两条底线：
- **不整表无上限地吐**：几十万行的台账会把接口和浏览器一起拖死，超限标 `truncated`；
- **只认 .xlsx**：老式 .xls 是另一种二进制格式，openpyxl 读不了，要明确说清楚。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: 单个工作表最多返回多少行；超了截断并标记
MAX_ROWS = 2000
#: 单个工作表最多返回多少列
MAX_COLS = 60


@dataclass
class SheetData:
    name: str
    rows: list[list[str]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "rows": self.rows, "truncated": self.truncated}


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # 1.0 → "1"，跟用户在 Excel 里看到的一致
    return str(value)


def read_sheets(path: Path, *, max_rows: int = MAX_ROWS, max_cols: int = MAX_COLS) -> list[SheetData]:
    """读所有工作表。读不出来时抛异常，由调用方翻成人话。"""
    from openpyxl import load_workbook

    # data_only=True：取公式的计算结果而不是公式本身（用户想看的是值）
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets: list[SheetData] = []
        for worksheet in workbook.worksheets:
            sheet = SheetData(name=worksheet.title)
            for index, row in enumerate(worksheet.iter_rows(values_only=True)):
                if index >= max_rows:
                    sheet.truncated = True
                    break
                cells = [_cell(v) for v in row[:max_cols]]
                if len(row) > max_cols:
                    sheet.truncated = True
                sheet.rows.append(cells)
            # 去掉尾部整行/整列空白：表格文件常带一堆空行，前端画出来全是空格
            while sheet.rows and not any(c.strip() for c in sheet.rows[-1]):
                sheet.rows.pop()
            sheets.append(sheet)
        return sheets
    finally:
        workbook.close()
