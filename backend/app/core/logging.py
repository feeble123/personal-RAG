"""统一日志配置（P2 单元2）。

- `setup_logging()`：按 `settings.log_level` 设根级别；`settings.log_json=True` 时挂 JSON handler
  （结构化单行 JSON，对接 Grafana/Loki 友好），否则维持默认纯文本格式（行为不变）。
- `JsonFormatter`：单行 JSON，字段 {ts, level, logger, message, exc_info}。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core.redact import redact

_JSON_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class RedactingFormatter(logging.Formatter):
    """纯文本 formatter 的脱敏包装：对最终格式化结果（含堆栈）整体脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON（结构化），并对 message / 堆栈脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc_info"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """按 settings 配置根 logger。幂等：重复调用不会重复挂 handler。

    测试环境（app_env=test）跳过：pytest 用 caplog 自己的 logging 机制捕获，
    setup_logging 挂 handler 会与 pytest 的 logging 插件冲突（全量跑时干扰 caplog）。
    """
    from app.core.config import settings

    if settings.app_env == "test":
        return

    level = (settings.log_level or "INFO").upper()
    if level not in _LOG_LEVELS:
        level = "INFO"

    root = logging.getLogger()
    root.setLevel(level)

    # 移除我们之前加的 handler（幂等），保留外部的（如 pytest 捕获）
    for h in list(root.handlers):
        if getattr(h, "_rag_managed", False):
            root.removeHandler(h)

    handler = logging.StreamHandler()
    handler._rag_managed = True  # type: ignore[attr-defined]  标记由本模块管理
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(RedactingFormatter(_JSON_FORMAT))
    root.addHandler(handler)
