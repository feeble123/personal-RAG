"""解析器工厂：按扩展名分发。

升级路径：新增格式只需实现 DocumentParser 并在此注册。
"""
from __future__ import annotations

from pathlib import Path

from app.core.exceptions import BizError
from app.services.parser.base import DocumentParser
from app.services.parser.docx_parser import DocxParser
from app.services.parser.excel_parser import CsvParser, ExcelParser
from app.services.parser.pdf import PDFParser
from app.services.parser.text_parser import MarkdownParser, TextParser

_REGISTRY: dict[str, DocumentParser] = {
    ext: parser
    for parser in (PDFParser(), DocxParser(), ExcelParser(), CsvParser(), MarkdownParser(), TextParser())
    for ext in parser.extensions
}

# 旧版 .doc 二进制格式：python-docx 不支持，需转换或用 win32com
_SUPPORTED = sorted(_REGISTRY)


def get_parser(filename: str) -> DocumentParser:
    ext = Path(filename).suffix.lower().lstrip(".")
    parser = _REGISTRY.get(ext)
    if not parser:
        raise BizError(f"不支持的文件格式 .{ext}，支持：{', '.join(_SUPPORTED)}", 400, "UNSUPPORTED_FORMAT")
    return parser


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower().lstrip(".") in _REGISTRY
