"""P2 单元2：可观测性·基础——结构化日志 + 请求中间件。

- JsonFormatter：输出单行 JSON，可 json.loads，含 ts/level/logger/message
- 请求中间件：请求后记录「请求 METHOD path status 耗时 client」行
- log_json 开关：切换后根 logger 挂 JsonFormatter handler
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import JsonFormatter, setup_logging


class TestJsonFormatter:
    def test_json_formatter_emits_json(self):
        """JsonFormatter 输出可 json.loads 的单行 JSON。"""
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello %s", args=("world",), exc_info=None,
        )
        line = JsonFormatter().format(record)
        data = json.loads(line)  # 不抛 = 合法 JSON
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "hello world"
        assert "ts" in data

    def test_json_formatter_includes_exc_info(self):
        """异常日志：exc_info 字段包含堆栈。"""
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                name="t", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="fail", args=(), exc_info=logging.sys.exc_info(),
            )
        data = json.loads(JsonFormatter().format(record))
        assert "boom" in data["exc_info"]


def _clear_managed_handlers() -> None:
    """清掉 setup_logging 挂的 _rag_managed handler（测试隔离）。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_rag_managed", False):
            root.removeHandler(h)


class TestSetupLogging:
    def test_setup_logging_idempotent(self, monkeypatch):
        """重复调用不重复挂 handler。"""
        from app.core.config import settings

        _clear_managed_handlers()
        monkeypatch.setattr(settings, "app_env", "development")  # test 环境 setup_logging 跳过
        monkeypatch.setattr(settings, "log_json", True)
        setup_logging()
        setup_logging()
        managed = [h for h in logging.getLogger().handlers if getattr(h, "_rag_managed", False)]
        assert len(managed) == 1

    def test_log_json_switch_adds_json_handler(self, monkeypatch):
        """log_json=True → 根 logger 有 JsonFormatter handler。"""
        from app.core.config import settings

        _clear_managed_handlers()
        monkeypatch.setattr(settings, "app_env", "development")
        monkeypatch.setattr(settings, "log_json", True)
        setup_logging()
        managed = [h for h in logging.getLogger().handlers if getattr(h, "_rag_managed", False)]
        assert any(isinstance(h.formatter, JsonFormatter) for h in managed)

    def test_plain_text_default(self, monkeypatch):
        """log_json=False（默认）→ 纯文本 handler（非 JsonFormatter）。"""
        from app.core.config import settings

        _clear_managed_handlers()
        monkeypatch.setattr(settings, "app_env", "development")
        monkeypatch.setattr(settings, "log_json", False)
        setup_logging()
        managed = [h for h in logging.getLogger().handlers if getattr(h, "_rag_managed", False)]
        assert all(not isinstance(h.formatter, JsonFormatter) for h in managed)

    def test_setup_logging_skipped_in_test_env(self, monkeypatch):
        """test 环境跳过 setup_logging（避免与 pytest logging 插件冲突）。"""
        from app.core.config import settings

        _clear_managed_handlers()
        monkeypatch.setattr(settings, "app_env", "test")
        setup_logging()
        managed = [h for h in logging.getLogger().handlers if getattr(h, "_rag_managed", False)]
        assert len(managed) == 0


class _ListHandler(logging.Handler):
    """收集日志记录到列表（测试用，独立于根 logger，不受全量跑状态影响）。"""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class TestRequestMiddleware:
    async def test_request_middleware_logs_line(self, client):
        """请求后记录「请求 METHOD path status 耗时」行（独立 handler 捕获，稳）。

        注意：pytest 全量跑时可能把 app.core.middleware logger 标记 disabled（其内部
        logging 插件行为），测试需临时启用该 logger 才能捕获——验证的是「中间件确实
        打了日志」而非 pytest 的 logger 状态。
        """
        logger = logging.getLogger("app.core.middleware")
        was_disabled = logger.disabled
        logger.disabled = False
        handler = _ListHandler()
        logger.addHandler(handler)
        try:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
        finally:
            logger.removeHandler(handler)
            logger.disabled = was_disabled
        lines = [r.getMessage() for r in handler.records
                 if r.getMessage().startswith("请求 GET /api/health")]
        assert lines, "应有请求日志行"
        assert any("200" in ln for ln in lines)
