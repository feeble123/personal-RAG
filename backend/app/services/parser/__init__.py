"""文档解析器：按扩展名分发到各格式解析器。"""
from __future__ import annotations

from .factory import get_parser

__all__ = ["get_parser"]
