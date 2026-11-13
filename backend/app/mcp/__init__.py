"""MCP（Model Context Protocol）薄封装：把本体查询暴露给外部 agent。

只做「翻译层」——把 MCP 工具调用转成 `services.graph.ontology` 的调用，
不在这里写任何业务逻辑。同一批查询的 REST 入口在 `modules/ontology/routes.py`，
两边共用 `services/graph/ontology/queries.py`。
"""
from __future__ import annotations

from app.mcp.server import create_server, main

__all__ = ["create_server", "main"]
