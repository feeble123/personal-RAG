"""本体 MCP 服务（stdio）。

启动：
    cd backend
    .venv\\Scripts\\python.exe -m app.mcp

工具（5 个，与 REST 一一对应）：

| 工具 | 作用 | 对应 REST |
|---|---|---|
| `ontology_search`         | 词 → 实体（本体实例 / 图节点） | GET /api/v1/ontology/search |
| `ontology_entity`         | 实体详情 + 1~2 跳邻居 + 证据 | GET /api/v1/ontology/entities/{uid} |
| `ontology_neighbors`      | 纯子图 | GET /api/v1/ontology/neighbors/{uid} |
| `get_parameter_priors`    | 参数推荐卡（范围/初值/依据） | GET /api/v1/ontology/params |
| `ontology_ask`            | 事实包，本体不够时自动回退向量检索 | POST /api/v1/ontology/ask |

两条硬约定：

1. **stdout 只能走协议帧**。本模块及其依赖不得 `print`，日志一律走 stderr
   （`logging` 默认就是 stderr）。
2. **失败也要有结构**。可预期的失败（实体不存在、图库未启用）返回
   `{"ok": false, "error": {"code", "message"}}`，让 agent 能分支处理；
   意外异常不吞，交给 MCP 客户端显示为工具错误。
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)

SERVER_NAME = "ontology-rag"
SERVER_VERSION = "1.0.0"

INSTRUCTIONS = """\
水利工程领域本体知识库（Ontology RAG）。提供两类知识：

1. 参数先验（Parameter）：模型参数的取值范围、初值、敏感度，每条都带依据
   （教材/手册/规范，能定位到原文切片）。**没有依据的数值不会返回**，
   返回 None 是正常结果，不要自己编一个数。
2. 概念与关系（图）：流域、工程节点、防洪链等实体及其关系。

推荐用法：
- 先 `ontology_search` 把中文名/参数名链接成 uid；
- 需要取值建议 → `get_parameter_priors`（可传 climate / flood_type 收窄范围）；
- 需要背景知识 → `ontology_ask`（本体事实不够时会自动回退全文检索）。

注意：`range_level` 表示范围强度（none < general < conditional < conflict）；
出现 `conflict` 时请把候选并列给用户，不要自己挑一个。"""


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _db():
    """stdio 模式不跑 FastAPI lifespan，所以这里自己开会话。"""
    from app.db.session import async_session_factory

    return async_session_factory()


def register_tools(server: MCPServer) -> None:
    """把 5 个查询注册成 MCP 工具（实现全部在 service 层）。"""
    from app.core.exceptions import BizError
    from app.services.graph.ontology import queries
    from app.services.graph.ontology.retrieval import retrieve_with_ontology

    @server.tool(
        description="把问题里的词（中文名/参数名/工程名）链接成本体实体，返回 uid 与匹配得分。"
    )
    async def ontology_search(
        q: str,
        kb_id: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        try:
            async with _db() as db:
                hits = await queries.search_entities(db, q, kb_id=kb_id, limit=limit)
        except BizError as exc:
            return _fail(exc.code, exc.message)
        return _ok(query=q, hits=hits)

    @server.tool(
        description="按 uid 取实体详情：属性、证据、1~2 跳邻居与关系边。"
    )
    async def ontology_entity(
        uid: str,
        depth: int = 1,
        include_evidence: bool = True,
    ) -> dict[str, Any]:
        try:
            async with _db() as db:
                detail = await queries.entity_detail(
                    db, uid, depth=depth, include_evidence=include_evidence
                )
        except BizError as exc:
            return _fail(exc.code, exc.message)
        return _ok(**detail)

    @server.tool(description="纯图查询：某个 uid 周围 1~2 跳的子图（节点 + 关系边）。")
    async def ontology_neighbors(
        uid: str,
        depth: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            return _ok(**await queries.graph_neighbors(uid, depth=depth, limit=limit))
        except BizError as exc:
            return _fail(exc.code, exc.message)

    @server.tool(
        description=(
            "参数先验推荐卡：取值范围、初值、敏感度与依据。"
            "可传 climate（湿润/半湿润/半干旱/干旱）、flood_type（单峰/多峰）收窄范围。"
            "range_level=conflict 时请并列候选，不要替用户选。"
        )
    )
    async def get_parameter_priors(
        names: list[str] | None = None,
        climate: str | None = None,
        flood_type: str | None = None,
        group: str | None = None,
        kb_id: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            async with _db() as db:
                cards = await queries.param_cards(
                    db,
                    names=names,
                    group=group,
                    kb_id=kb_id,
                    climate=climate,
                    flood_type=flood_type,
                    limit=limit,
                )
        except BizError as exc:
            return _fail(exc.code, exc.message)
        return _ok(cards=cards, count=len(cards))

    @server.tool(
        description=(
            "本体优先问答：返回事实包（facts / evidence / sources）。"
            "本体事实不足阈值时会回退向量检索，此时 fallback_used=true。"
        )
    )
    async def ontology_ask(
        question: str,
        kb_id: int | None = None,
        min_facts: int | None = None,
    ) -> dict[str, Any]:
        try:
            async with _db() as db:
                pack = await retrieve_with_ontology(
                    db, question, kb_id=kb_id, min_facts=min_facts
                )
        except BizError as exc:
            return _fail(exc.code, exc.message)
        return _ok(**pack.to_dict())


def create_server() -> MCPServer:
    """建服务实例（测试里也用它，不启传输层）。"""
    server = MCPServer(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=SERVER_VERSION,
    )
    register_tools(server)
    return server


def main(transport: str = "stdio") -> None:
    from app.core.logging import setup_logging

    # 日志走 stderr，绝不会污染 stdio 协议帧
    setup_logging()
    logger.info("本体 MCP 服务启动（transport=%s）", transport)
    create_server().run(transport)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "stdio")
