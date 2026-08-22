"""Excel / CSV 解析：按 sheet 结构化切片（单独成块，不入正文）。"""
from __future__ import annotations

import csv
from pathlib import Path

from app.services.parser.base import DocumentParser, ParsedBlock, ParsedDocument


def _row_to_line(cells: list[str]) -> str:
    return " | ".join(str(c).strip() for c in cells)


class ExcelParser(DocumentParser):
    extensions = ("xlsx", "xls")

    def parse(self, path: Path, filename: str, chunk_strategy: str = "old") -> ParsedDocument:
        import pandas as pd  # 懒加载

        quality: dict = {"parser": "excel", "sheets": 0, "rows": 0}
        blocks: list[ParsedBlock] = []

        sheets = pd.read_excel(path, sheet_name=None, dtype=str, keep_default_na=False, engine=None)
        for sheet_name, frame in sheets.items():
            quality["sheets"] += 1
            if frame.empty:
                continue
            header = [str(c).strip() for c in frame.columns]
            section = f"{filename} / {sheet_name}"
            # 表头行
            blocks.append(ParsedBlock(text=_row_to_line(header), section=section, block_type="table"))
            for _, row in frame.iterrows():
                line = _row_to_line([str(c).strip() for c in row.values])
                if line.strip(" |"):
                    blocks.append(ParsedBlock(text=line, section=section, block_type="table"))
                    quality["rows"] += 1

        quality["blocks"] = len(blocks)
        return ParsedDocument(blocks=blocks, quality=quality)


class CsvParser(DocumentParser):
    extensions = ("csv",)

    def parse(self, path: Path, filename: str, chunk_strategy: str = "old") -> ParsedDocument:
        quality: dict = {"parser": "csv", "rows": 0}
        blocks: list[ParsedBlock] = []
        section = filename

        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            for row in reader:
                line = _row_to_line(row)
                if line.strip(" |"):
                    blocks.append(ParsedBlock(text=line, section=section, block_type="table"))
                    quality["rows"] += 1

        quality["blocks"] = len(blocks)
        return ParsedDocument(blocks=blocks, quality=quality)
