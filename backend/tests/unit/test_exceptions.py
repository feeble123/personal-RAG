"""P2 单元2：可观测性·基础——全局异常处理器补日志。

- 未捕获异常 → 记日志（含堆栈），不再静默吞错
- 生产环境（debug=False）→ 响应隐藏内部堆栈
"""
from __future__ import annotations

import logging

from app.core.exceptions import register_exception_handlers


class _App:
    """最小 FastAPI 兼容对象：模拟 exception_handler 装饰器 + add_exception_handler。"""

    def __init__(self):
        self.user_middleware = []
        self.exception_handlers = {}
        self.router = type("R", (), {})()
        self.router.exception_handlers = self.exception_handlers

    def exception_handler(self, exc_cls):
        """装饰器形式：register_exception_handlers 用 @app.exception_handler(X)。"""

        def deco(fn):
            self.exception_handlers[exc_cls] = fn
            return fn

        return deco

    def add_exception_handler(self, exc_cls, handler):
        self.exception_handlers[exc_cls] = handler


async def _call_handler(handler, request, exc):
    return await handler(request, exc)


class _ListHandler(logging.Handler):
    """收集日志记录到列表（独立于根 logger，不受全量跑状态影响）。"""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class TestUnhandledErrorLogged:
    async def test_unhandled_error_logged(self):
        """未捕获异常 → logger 记录「未处理异常」+ 路径；响应结构 {code,message}。

        注意：pytest 全量跑时可能把 app.core.exceptions logger 标记 disabled（其内部
        logging 插件行为），测试需临时启用该 logger——验证的是「异常处理器确实打了
        日志」而非 pytest 的 logger 状态。
        """
        app = _App()
        register_exception_handlers(app)  # type: ignore[arg-type]
        handler = app.exception_handlers[Exception]

        logger = logging.getLogger("app.core.exceptions")
        was_disabled = logger.disabled
        logger.disabled = False
        list_handler = _ListHandler()
        logger.addHandler(list_handler)
        try:
            from starlette.requests import Request

            scope = {"type": "http", "method": "POST", "path": "/api/qa", "headers": []}
            request = Request(scope)
            resp = await _call_handler(handler, request, RuntimeError("boom"))
        finally:
            logger.removeHandler(list_handler)
            logger.disabled = was_disabled

        assert resp.status_code == 500
        body = resp.body.decode()
        assert "INTERNAL_ERROR" in body

        # 关键：异常被记录，不再静默
        logged = [r.getMessage() for r in list_handler.records]
        assert any("未处理异常" in m and "/api/qa" in m for m in logged), logged

    async def test_500_hides_detail_in_prod(self):
        """debug=False（生产）→ 响应 message 为通用文案，不泄露堆栈。"""
        app = _App()
        register_exception_handlers(app)  # type: ignore[arg-type]
        handler = app.exception_handlers[Exception]

        from starlette.requests import Request

        scope = {"type": "http", "method": "GET", "path": "/api/x", "headers": []}
        resp = await _call_handler(handler, Request(scope), RuntimeError("secret_detail"))
        body = resp.body.decode()
        assert "secret_detail" not in body
        assert "服务器内部错误" in body
