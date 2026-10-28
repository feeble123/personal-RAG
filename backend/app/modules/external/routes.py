"""对外检索 API（单元 1）：供业务系统/未来 Agent 服务间调用。

- 鉴权：X-API-Key（配置项 EXTERNAL_API_KEY），不复用用户 JWT
- 契约：沿用 app/services/retriever.py 的稳定结构（字段名已冻结）
- 只读：不写库、不改索引
- 限流：按 IP（EXTERNAL_RATE_LIMIT，默认 120/分钟）

路径：POST /api/v1/search、GET /api/v1/knowledge-bases
"""
from __future__ import annotations

import logging
from time import perf_counter

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import DbSession, verify_external_api_key
from app.core.ratelimit import limiter
from app.db.models import KnowledgeBase
from app.services import retriever

logger = logging.getLogger(__name__)

# router 级鉴权：本模块所有端点都必须带有效 X-API-Key
router = APIRouter(
    prefix="/v1",
    tags=["external"],
    dependencies=[Depends(verify_external_api_key)],
)


class SearchIn(BaseModel):
    """外部检索请求（契约输入）。"""

    query: str = Field(..., min_length=1, max_length=500, description="检索问题")
    top_k: int = Field(5, ge=1, le=20, description="返回条数（1~20）")
    kb_id: int | None = Field(None, description="限定知识库 id；缺省跨全库")
    doc_ids: list[int] | None = Field(None, description="限定文档 id 列表（可选）")


@router.post("/search")
@limiter.limit(settings.external_rate_limit)
async def external_search(request: Request, body: SearchIn) -> dict:
    """检索知识库，返回「片段 + 出处」稳定契约。

    不记录 query 原文（业务问题可能含敏感信息），只记录长度与检索规模。
    """
    t0 = perf_counter()
    results = await retriever.retrieve(
        body.query,
        top_k=body.top_k,
        kb_id=body.kb_id,
        doc_ids=body.doc_ids,
    )
    elapsed_ms = (perf_counter() - t0) * 1000
    logger.info(
        "外部检索 query_len=%s kb_id=%s top_k=%s hits=%s elapsed_ms=%.1f",
        len(body.query), body.kb_id, body.top_k, len(results), elapsed_ms,
    )
    return retriever.results_to_dict(results)


@router.get("/knowledge-bases")
async def list_knowledge_bases(db: DbSession) -> dict:
    """列出可用知识库（业务系统据此选择 kb_id）。只返回 id/名称/规模。"""
    rows = (
        await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))
    ).scalars().all()
    return {
        "items": [
            {
                "id": kb.id,
                "name": kb.name,
                "doc_count": kb.doc_count,
                "chunk_count": kb.chunk_count,
            }
            for kb in rows
        ]
    }
