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
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.db.models import KnowledgeBase
from app.db.session import async_session_factory
from app.services import rag, retriever
from app.services.chat import (
    ANSWER_STYLES,
    DEFAULT_STYLE,
    STYLE_CONFIG,
    build_chat_model,
    build_prompt,
)

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


class ChatIn(BaseModel):
    """对外问答请求。"""

    question: str = Field(..., min_length=1, max_length=500, description="问题")
    kb_id: int | None = Field(None, description="限定知识库 id；缺省跨全库")
    top_k: int = Field(5, ge=1, le=10, description="送入生成的知识片段数（1~10）")
    style: str = Field(
        DEFAULT_STYLE,
        description="回答风格：standard/logical/summary/expanded/tutorial；非法值回退 standard",
    )


@router.post("/chat")
@limiter.limit(settings.external_chat_rate_limit)
async def external_chat(request: Request, body: ChatIn) -> dict:
    """对外问答：问题 → 完整回答 + 引用（非流式）。

    - 不写用户会话/消息（外部调用没有用户身份），只记调用日志
    - 不读写问答记忆/语义缓存，避免跨调用污染
    - 返回证据等级，调用方可据此决定是否采信
    """
    t0 = perf_counter()

    async with async_session_factory() as db:
        cites = await rag.retrieve(db, body.question, kb_id=body.kb_id, top_k=body.top_k)
    scores = [c.score for c in cites if c.score is not None]
    evidence_level = rag.judge_evidence_level(scores)

    style = body.style if body.style in ANSWER_STYLES else DEFAULT_STYLE
    temperature = float(STYLE_CONFIG[style]["temperature"])
    llm = build_chat_model(temperature)
    messages = build_prompt(
        body.question,
        cites,
        history=None,
        style=style,
        evidence_weak=(evidence_level in ("weak", "none")),
    )

    pieces: list[str] = []
    usage_in = usage_out = None
    try:
        async for chunk in llm.astream(messages):
            text = chunk.content
            if isinstance(text, list):
                text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
            if text:
                pieces.append(text)
            usage = getattr(chunk, "usage_metadata", None)
            if usage:
                usage_in = usage.get("input_tokens") or usage_in
                usage_out = usage.get("output_tokens") or usage_out
    except Exception:
        logger.exception("外部问答生成失败")
        raise BizError("生成失败，请稍后重试", 502, "GENERATION_FAILED")

    answer = "".join(pieces)
    results = retriever.results_from_chunks(cites)
    elapsed_ms = (perf_counter() - t0) * 1000
    logger.info(
        "外部问答 question_len=%s kb_id=%s hits=%s evidence=%s in=%s out=%s elapsed_ms=%.1f",
        len(body.question), body.kb_id, len(cites), evidence_level,
        usage_in, usage_out, elapsed_ms,
    )
    return {
        "answer": answer,
        "citations": [r.to_dict() for r in results],
        "evidence_level": evidence_level,
        "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
    }
