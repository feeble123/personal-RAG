"""MCP 薄封装：工具注册、参数透传、失败结构。

测的是「MCP 客户端看到的东西」：`server.call_tool()` 的返回值，
而不是直接调内部函数——注册漏了、参数名对不上，这里就会红。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from app.mcp.server import create_server

EXPECTED_TOOLS = {
    "ontology_search",
    "ontology_entity",
    "ontology_neighbors",
    "get_parameter_priors",
    "ontology_ask",
}


def _payload(result: Any) -> dict:
    """CallToolResult → dict。文本内容就是工具返回的 JSON。"""
    assert getattr(result, "isError", None) is not True, result
    assert result.content, "工具没有任何返回内容"
    return json.loads(result.content[0].text)


@pytest.fixture(scope="module")
def server():
    return create_server()


@pytest_asyncio.fixture
async def sample_param(_ensure_test_tables):
    """一条带可溯源证据的参数实例，用完删掉。"""
    from app.db.session import async_session_factory
    from app.services.graph.ontology import instances as inst_mod

    draft = inst_mod.InstanceDraft(
        profile_id="tuning",
        entity_type="Parameter",
        name="mcp_sm",
        uid="pn:mcp_sm",
        props={
            "display_zh": "自由水蓄水能力",
            "group": "水源划分",
            "unit": "mm",
            "range_low": 5,
            "range_high": 50,
            "initial": 10,
        },
        evidence=[
            {
                "type": "手册",
                "note": "手册参数表",
                "doc_id": 20,
                "chunk_id": 28192,
                "traceable": True,
            }
        ],
    )
    async with async_session_factory() as db:
        await inst_mod.upsert_instances(db, [draft])
        await db.commit()
    yield draft
    async with async_session_factory() as db:
        row = await inst_mod.get_instance(db, "tuning", "Parameter", "mcp_sm", 0)
        if row is not None:
            await inst_mod.delete_instance(db, row)
            await db.commit()


class TestRegistration:
    async def test_five_tools_registered(self, server):
        tools = await server.list_tools()
        assert {t.name for t in tools} == EXPECTED_TOOLS

    async def test_every_tool_has_description(self, server):
        for tool in await server.list_tools():
            assert tool.description, f"{tool.name} 没有说明，agent 不知道什么时候该调它"


class TestTools:
    async def test_param_priors_returns_card(self, server, sample_param):
        data = _payload(
            await server.call_tool("get_parameter_priors", {"names": ["mcp_sm"]})
        )
        assert data["ok"] is True
        assert data["count"] == 1
        card = data["cards"][0]
        assert card["range"] == [5.0, 50.0]
        assert card["evidence_level"] == "traceable"

    async def test_search_links_instance(self, server, sample_param):
        data = _payload(await server.call_tool("ontology_search", {"q": "mcp_sm 取多少"}))
        assert data["ok"] is True
        assert any(h["name"] == "mcp_sm" for h in data["hits"])

    async def test_entity_detail(self, server, sample_param):
        data = _payload(
            await server.call_tool("ontology_entity", {"uid": "pn:mcp_sm"})
        )
        assert data["ok"] is True
        assert data["instance"]["name"] == "mcp_sm"
        assert data["evidence"][0]["note"] == "手册参数表"

    async def test_ask_returns_fact_pack(self, server, sample_param):
        data = _payload(
            await server.call_tool(
                "ontology_ask", {"question": "mcp_sm 怎么取", "min_facts": 1}
            )
        )
        assert data["ok"] is True
        assert data["question"]
        assert any(f["kind"] == "parameter_prior" for f in data["facts"])
        assert "fallback_used" in data


class TestFailureStructure:
    """可预期失败要有 code/message，agent 才能分支处理。"""

    async def test_entity_not_found(self, server):
        data = _payload(await server.call_tool("ontology_entity", {"uid": "pn:nope"}))
        assert data["ok"] is False
        assert data["error"]["code"] == "ONTOLOGY_ENTITY_NOT_FOUND"
        assert data["error"]["message"]

    async def test_neighbors_graph_disabled(self, server):
        data = _payload(await server.call_tool("ontology_neighbors", {"uid": "pn:nope"}))
        assert data["ok"] is False
        assert data["error"]["code"] == "GRAPH_DISABLED"

    async def test_missing_required_arg_is_rejected(self, server):
        """参数校验交给 MCP 层：缺 question 不该进业务代码。"""
        from mcp.server.mcpserver.exceptions import ToolError

        with pytest.raises(ToolError, match="question"):
            await server.call_tool("ontology_ask", {})
