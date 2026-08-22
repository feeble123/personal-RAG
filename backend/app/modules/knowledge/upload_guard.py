"""上传文件内容校验（P0-10 单元1）：magic number + zip bomb + 文本安全。

问题：上传只校验扩展名，`evil.pdf`（实际是 MZ 可执行文件）会被当 PDF 解析入库。
方案：写完文件后、建 DB 记录前，用文件头签名（magic bytes）验证「内容与扩展名匹配」，
     并对 docx/xlsx（zip 容器）做解压炸弹防护。

单元2 会在解析前二次验证时复用这里的 `verify_file`。
"""
from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from app.core.exceptions import BizError

# ---- 文件头签名（magic bytes）----
# pdf 固定 `%PDF`；docx/xlsx 都是 zip 容器 `PK\x03\x04`；md/txt/csv 是纯文本无强签名。
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"

# 有强签名的格式：扩展名 -> 期望的文件头（读前 8 字节足够）
_SIGNATURES: dict[str, bytes] = {
    "pdf": _PDF_MAGIC,
    "docx": _ZIP_MAGIC,
    "xlsx": _ZIP_MAGIC,
}

# 纯文本格式：允许任意内容，但拒绝明显是二进制的（含 NUL 字节）
_TEXT_FORMATS = {"md", "markdown", "txt", "csv"}

# zip bomb 防护：解压后总大小上限（默认 200MB，与上传上限一致）与最大压缩比（放 500 倍）
MAX_UNCOMPRESSED_SIZE = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500


def _check_signature(ext: str, path: Path) -> None:
    """按扩展名校验文件头签名；无强签名格式（纯文本）跳过。"""
    expected = _SIGNATURES.get(ext)
    if expected is None:
        return
    try:
        with path.open("rb") as f:
            head = f.read(len(expected))
    except OSError:
        raise BizError("文件读取失败，无法校验内容", 400, "INVALID_FILE")
    if not head.startswith(expected):
        raise BizError(
            f"文件内容与 .{ext} 格式不符，已被拦截（可能是伪造扩展名或损坏文件）",
            400,
            "CONTENT_MISMATCH",
        )


def _check_zip_bomb(path: Path) -> None:
    """docx/xlsx 是 zip：校验解压后总大小 / 压缩比，拦截 zip bomb。"""
    try:
        with ZipFile(path) as zf:
            total = 0
            for info in zf.infolist():
                if info.file_size > MAX_UNCOMPRESSED_SIZE:
                    raise BizError("解压后内容过大（超过 200MB 限制）", 413, "ZIP_BOMB")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_SIZE:
                    raise BizError("解压后内容过大（超过 200MB 限制）", 413, "ZIP_BOMB")
                # 压缩比防护：单个条目解压后 > 源大小 * 500 倍 → 极可能是炸弹
                if info.compress_size > 0 and info.file_size > info.compress_size * MAX_COMPRESSION_RATIO:
                    raise BizError("文件压缩比异常（疑似 zip 炸弹）", 413, "ZIP_BOMB")
    except BadZipFile:
        raise BizError("文件已损坏或不是有效的 Office 文档", 400, "BAD_ZIP")


def _check_text_is_not_binary(path: Path) -> None:
    """纯文本格式（md/txt/csv）：拒绝含 NUL 字节的二进制伪装。"""
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        raise BizError("文件读取失败，无法校验内容", 400, "INVALID_FILE")
    if b"\x00" in head:
        raise BizError("文件内容不是有效文本（疑似二进制伪装）", 400, "BINARY_TEXT")


def verify_file(ext: str, path: Path) -> None:
    """上传/解析前的完整校验入口。

    - pdf / docx / xlsx：校验 magic bytes
    - docx / xlsx：额外 zip bomb 防护
    - md / markdown / txt / csv：拒绝二进制伪装
    """
    _check_signature(ext, path)
    if ext in ("docx", "xlsx"):
        _check_zip_bomb(path)
    elif ext in _TEXT_FORMATS:
        _check_text_is_not_binary(path)
