"""请求日志中间件（P2 单元2）。

记录每个 HTTP 请求：方法 / 路径 / 状态码 / 耗时 / 客户端 IP。
- 不消费 body（SSE 流式安全）：只 await call_next 拿 response，不读 request.stream()
- 作为最外层中间件（CORS 之后 add），能罩住 GZip/SlowAPI 产生的 429/OPTIONS
- P2 单元3 的 Prometheus 请求指标也在这里埋点（见 core/metrics.py）
"""
from __future__ import annotations

import logging
from time import perf_counter

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 异常已由全局 handler 处理（会记日志），这里只补一条请求级别的失败留痕
            dur_ms = (perf_counter() - start) * 1000
            client = request.client.host if request.client else "?"
            logger.warning("请求 %s %s 异常 %sms client=%s", request.method, request.url.path, f"{dur_ms:.1f}", client)
            raise
        dur_ms = (perf_counter() - start) * 1000
        client = request.client.host if request.client else "?"
        logger.info("请求 %s %s %s %sms client=%s", request.method, request.url.path, response.status_code, f"{dur_ms:.1f}", client)
        return response
