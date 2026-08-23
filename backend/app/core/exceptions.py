"""统一业务异常 + 全局异常处理器。

自定义 `BizError` 携带状态码与可读消息，由全局 handler 统一序列化，
前端只需解析 {code, message} 结构即可。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class BizError(Exception):
    """业务异常：status_code 为 HTTP 状态码，message 为可读提示。"""

    def __init__(self, message: str, status_code: int = 400, code: str = "BAD_REQUEST"):
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "HTTP_ERROR", "message": str(exc.detail)},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # P2 单元2：从「静默吞错」到「留现场」——未捕获异常必须记日志（含堆栈）
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        # 生产环境不泄露内部堆栈；开发环境保留便于调试
        from app.core.config import settings

        detail = str(exc) if settings.debug else "服务器内部错误，请稍后重试"
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": detail},
        )
