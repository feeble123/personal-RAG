"""Excel / CSV 解析：按 sheet 结构化切片（单独成块，不入正文）。"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from app.services.parser.base import DocumentParser, ParsedBlock, ParsedDocument


def _row_to_line(cells: list[str]) -> str:
    return " | ".join(str(c).strip() for c in cells)


# 单元二 子单元③：Excel 日期值规范化。
# pandas 用 dtype=str 读 datetime 列时，把真日期单元格转成 "2025-09-01 00:00:00" 这种
# 带时间戳的 ISO 串；而文本日期单元格是 "2025.9.15" 点分串。同一份文档两种格式打架、
# 带噪声、不可排序。这里统一：
#   1) 剥掉 " 00:00:00" 时间戳后缀（纯日期不该带时刻）
#   2) 日期统一成 ISO 短格式 "YYYY-MM-DD"（"2025.9.15" → "2025-09-15"），可排序可精确筛选
# 非日期的普通值原样返回。
_DATE_TIME_SUFFIX_RE = re.compile(r"\s+00:00:00(?:\.\d+)?$")
_DOTTED_DATE_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _normalize_excel_cell(value) -> str:
    """把单个 Excel 单元格转成规范文本（日期统一 YYYY-MM-DD，去时刻噪声）。"""
    if value is None:
        return ""
    if hasattr(value, "strftime"):  # pandas 读 datetime 列时可能给 Timestamp
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    s = _DATE_TIME_SUFFIX_RE.sub("", s)  # "2025-09-01 00:00:00" → "2025-09-01"
    m = _ISO_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = _DOTTED_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def _normalize_excel_row(cells: list) -> list[str]:
    """把一行单元格逐个规范化（日期统一格式），供 _row_to_line 与 elements 共用。"""
    return [_normalize_excel_cell(c) for c in cells]


class ExcelParser(DocumentParser):
    extensions = ("xlsx", "xls")

    def parse(
        self, path: Path, filename: str, chunk_strategy: str = "old", parse_mode: str = "fast"
    ) -> ParsedDocument:
        import pandas as pd  # 懒加载

        quality: dict = {"parser": "excel", "sheets": 0, "rows": 0}
        blocks: list[ParsedBlock] = []

        sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False, engine=None)
        for sheet_name, frame in sheets.items():
            quality["sheets"] += 1
            if frame.empty:
                continue
            header = _normalize_excel_row([c for c in frame.columns])
            section = f"{filename} / {sheet_name}"
            # 表头行
            blocks.append(ParsedBlock(text=_row_to_line(header), section=section, block_type="table"))
            for _, row in frame.iterrows():
                cells = _normalize_excel_row([c for c in row.values])
                line = _row_to_line(cells)
                if line.strip(" |"):
                    blocks.append(ParsedBlock(text=line, section=section, block_type="table"))
                    quality["rows"] += 1

        quality["blocks"] = len(blocks)
        # P1-1：blocks → IR elements（Excel 每行是 TABLE，无标题层级）
        elements = [b.to_element(i, "excel") for i, b in enumerate(blocks)]
        return ParsedDocument(blocks=blocks, quality=quality, elements=elements)


class CsvParser(DocumentParser):
    extensions = ("csv",)

    def parse(
        self, path: Path, filename: str, chunk_strategy: str = "old", parse_mode: str = "fast"
    ) -> ParsedDocument:
        quality: dict = {"parser": "csv", "rows": 0}
        blocks: list[ParsedBlock] = []
        section = filename

        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            for row in reader:
                cells = _normalize_excel_row(row)
                line = _row_to_line(cells)
                if line.strip(" |"):
                    blocks.append(ParsedBlock(text=line, section=section, block_type="table"))
                    quality["rows"] += 1

        quality["blocks"] = len(blocks)
        # P1-1：CSV 同 Excel，每行是 TABLE
        elements = [b.to_element(i, "csv") for i, b in enumerate(blocks)]
        return ParsedDocument(blocks=blocks, quality=quality, elements=elements)
