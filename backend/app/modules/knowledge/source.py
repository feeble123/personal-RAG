"""原文文件访问：给前端 PDF 阅读器发**短期签名 URL**。

为什么不直接把登录 token 拼在 URL 上：
- pdf.js 在浏览器内部发起 fetch，塞不进 `Authorization` 头；
- 把 access token 放进 query 会进访问日志、浏览器历史、Referer，泄漏面太大。

所以发一个**只对单个文档有效、5 分钟过期**的 HMAC 签名。真泄漏了也只能读到
那一份文件，而且很快失效。签名密钥复用 `settings.jwt_secret`（生产环境必须固定，
否则后端重启后旧链接失效——TTL 只有 5 分钟，影响可忽略）。

Range 支持：Starlette 的 `FileResponse` 自带 `Accept-Ranges` 与 206 处理，
pdf.js 因此可以按需拉取分段，不必先下载整本 63MB 的书。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import BizError
from app.db.models import Chunk, Document
from app.services.source_locate import locate_in_page
from app.services.source_office import read_sheets

router = APIRouter(prefix="/documents", tags=["document-source"])

#: 签名有效期（秒）。够 pdf.js 完成首屏加载，短到不值得被转发。
TOKEN_TTL = 300
#: 有效期上限。阅读器长坐时最多给到 30 分钟（见 `create_file_token`）。
MAX_TOKEN_TTL = 1800

#: 后缀 → Content-Type。**白名单**：没列出来的后缀一律不给下载，
#: 免得哪天有人把 .html / .exe 传进来变成 XSS 或可执行载体。
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _secret() -> bytes:
    """签名密钥。开发环境没配 JWT_SECRET 时 settings 给随机值（重启即失效）。"""
    secret = (settings.jwt_secret or "").strip()
    if not secret:  # pragma: no cover - config 会兜底生成，走到这里说明配置被改坏
        raise BizError("服务端未配置签名密钥", 500, "SOURCE_SIGN_DISABLED")
    return secret.encode("utf-8")


def _signature(doc_id: int, expires_at: int, user_id: int) -> str:
    payload = f"{doc_id}:{expires_at}:{user_id}".encode()
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()[:40]


def issue_token(doc_id: int, user_id: int, *, ttl: int = TOKEN_TTL) -> str:
    """生成 `<过期时间戳>.<签发人 id>.<签名>`，三段都可读，便于排查。

    ttl 允许为负（生成已过期的 token）——测试要用它验证过期分支，
    把 ttl 夹到正数就永远测不到那条路径了。
    """
    expires_at = int(time.time()) + int(ttl)
    return f"{expires_at}.{user_id}.{_signature(doc_id, expires_at, user_id)}"


def verify_token(doc_id: int, token: str) -> None:
    """校验签名与有效期。

    不需要登录态——签名本身就是凭证（pdf.js 的分段请求既不带 cookie 也不带
    Authorization 头）。把签发人 id 放进 token 只是为了签名能重算，不是信任输入。
    """
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        raise BizError("链接无效", 403, "SOURCE_TOKEN_INVALID")
    expires_at, user_id, signature = int(parts[0]), int(parts[1]), parts[2]
    if not hmac.compare_digest(signature, _signature(doc_id, expires_at, user_id)):
        raise BizError("链接无效", 403, "SOURCE_TOKEN_INVALID")
    if expires_at < int(time.time()):
        raise BizError("链接已过期，请重新打开原文", 403, "SOURCE_TOKEN_EXPIRED")


def resolve_source_path(doc: Document) -> Path:
    """把 `stored_path` 解析成磁盘真实路径，顺手做三道防御。"""
    root = settings.upload_dir_path.resolve()
    path = (root / doc.stored_path).resolve()
    # 防目录穿越：stored_path 来自数据库，但数据库也可能被写脏
    if root not in path.parents:
        raise BizError("非法的文件路径", 400, "SOURCE_PATH_INVALID")
    suffix = path.suffix.lower()
    if suffix not in _MEDIA_TYPES:
        raise BizError(
            f"不支持在线查看 {suffix or '该'} 类型的文件", 415, "SOURCE_TYPE_UNSUPPORTED"
        )
    if not path.is_file():
        # 副本迁移时最常见的坑：库搬了、uploads 没搬
        raise BizError(
            "原文文件不在服务器上（数据库有记录，但 uploads 目录里没有这份文件）",
            410,
            "SOURCE_FILE_MISSING",
        )
    return path


@router.post("/{doc_id}/file-token")
async def create_file_token(
    doc_id: int,
    db: DbSession,
    user: CurrentUser,
    ttl: int = Query(TOKEN_TTL, ge=60, le=MAX_TOKEN_TTL, description="有效期（秒）"),
) -> dict:
    """换取一份短期可用的原文链接（登录用户即可，与问答的可见范围一致）。

    `ttl` 可调是因为 pdf.js 按需分段拉取：用户把阅读器挂着翻半小时，中途
    签名过期会让后面的页直接 403。阅读器传 30 分钟，其他场景用默认 5 分钟。
    上限 `MAX_TOKEN_TTL` 是硬约束——这道门是**免登录**的，别让它变成长期通行证。
    """
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise BizError("文档不存在", 404, "DOCUMENT_NOT_FOUND")
    path = resolve_source_path(doc)
    return {
        "doc_id": doc.id,
        "filename": doc.filename,
        "file_type": doc.file_type,
        "file_size": path.stat().st_size,
        "page_count": doc.page_count,
        "url": f"/api/documents/{doc_id}/file?t={issue_token(doc_id, user.id, ttl=ttl)}",
        "expires_in": ttl,
    }


# HEAD 必须一起收：浏览器内置预览、部分 PDF 阅读器会先探一次头拿长度，
# FastAPI 的 GET 路由不会自动应答 HEAD（实测 405），探活失败就当整份不可用。
@router.api_route("/{doc_id}/file", methods=["GET", "HEAD"])
async def get_source_file(doc_id: int, t: str, db: DbSession) -> FileResponse:
    """按签名 URL 输出原文件（`Accept-Ranges: bytes`，支持 206 分段）。"""
    verify_token(doc_id, t)
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise BizError("文档不存在", 404, "DOCUMENT_NOT_FOUND")
    path = resolve_source_path(doc)
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES[path.suffix.lower()],
        filename=doc.filename,
        content_disposition_type="inline",
        # 千万别让浏览器缓存这些响应：一本 70MB 的书会被 Chromium 当成
        # 超大缓存项，写入磁盘缓存失败就抛 net::ERR_CACHE_WRITE_FAILURE，
        # pdf.js 收到的是 network error，整本书直接打不开（实测复现）。
        # 链接本身只有 30 分钟寿命，缓存也没有复用价值。
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{doc_id}/locate")
async def locate_source(
    doc_id: int,
    db: DbSession,
    t: str = Query(..., description="与 /file 同一份签名，见模块说明"),
    page: int | None = Query(None, ge=1, description="PDF 物理页序（1 起）"),
    quote: str | None = Query(None, description="要框出来的原文引用"),
    chunk_id: int | None = Query(None, description="给切片 id 就自动取页码与块坐标"),
) -> dict:
    """把一条引用/切片定位到原 PDF 的具体位置，返回该页与高亮矩形。

    前端拿到 `rects`（PDF 点坐标）后按 viewport 缩放画浮层即可；
    `source` 说明这个框有多可信：`text` 精确到行、`bbox` 只是块、`none` 什么都没框到。
    """
    # 与 /file 用同一份签名：阅读器手里只有它（pdf.js 带不了 Authorization 头），
    # 再要求登录态只会让前端两套凭证并存。
    verify_token(doc_id, t)
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise BizError("文档不存在", 404, "DOCUMENT_NOT_FOUND")
    path = resolve_source_path(doc)
    if path.suffix.lower() != ".pdf":
        raise BizError(
            "只有 PDF 能定位到原文件位置；docx/xlsx 请用切片视图",
            415,
            "SOURCE_LOCATE_UNSUPPORTED",
        )

    printed_page = None
    chunk_bbox = None
    if chunk_id is not None:
        chunk = await db.get(Chunk, chunk_id)
        if chunk is None:
            raise BizError("切片不存在（文档可能重灌过）", 404, "CHUNK_NOT_FOUND")
        if chunk.doc_id != doc_id:
            raise BizError("切片不属于这份文档", 400, "CHUNK_DOC_MISMATCH")
        printed_page = chunk.printed_page
        chunk_bbox = chunk.bbox
        page = page or chunk.page
        quote = quote or _quote_of_chunk(chunk)
    if not page:
        raise BizError("需要 page 或 chunk_id 才能定位", 400, "LOCATE_PAGE_REQUIRED")

    result = locate_in_page(path, page, quote=quote, chunk_bbox=chunk_bbox)
    return {
        "doc_id": doc.id,
        "filename": doc.filename,
        "printed_page": printed_page,
        **result.to_dict(),
    }


@router.get("/{doc_id}/sheets")
async def get_source_sheets(doc_id: int, t: str, db: DbSession) -> dict:
    """Excel 原文：直接给网格数据，前端画成可滚动表格并定位到那一行。

    docx 不走这里——那类要保留版式，前端用 docx-preview 自己排版。
    """
    verify_token(doc_id, t)
    doc = await db.get(Document, doc_id)
    if doc is None:
        raise BizError("文档不存在", 404, "DOCUMENT_NOT_FOUND")
    path = resolve_source_path(doc)
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise BizError("只有 Excel 文件需要表格视图", 415, "SOURCE_SHEETS_UNSUPPORTED")
    try:
        sheets = read_sheets(path)
    except Exception as exc:  # noqa: BLE001 - 用户上传的文件，任何解析错误都要可读
        raise BizError(f"读不出这个 Excel：{exc}", 400, "SOURCE_SHEETS_INVALID") from exc
    return {
        "doc_id": doc.id,
        "filename": doc.filename,
        "sheets": [sheet.to_dict() for sheet in sheets],
    }


def _quote_of_chunk(chunk: Chunk) -> str | None:
    """切片没给引用时，用正文开头去定位（空白的就不找了）。"""
    text = (chunk.content or "").strip()
    return text[:200] if text else None
