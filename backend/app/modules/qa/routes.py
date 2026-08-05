"""SSE 流式问答路由。

事件协议（text/event-stream，每行 `data: {json}\n\n`）：
- {"event":"citations","data":[{CitationOut}...]}   先于正文
- {"event":"delta","data":"token片段"}              逐 token
- {"event":"done","data":{"message_id":88}}
- {"event":"error","data":"错误消息"}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.db.models import Citation, Conversation, Message
from app.db.session import async_session_factory
from app.modules.conversations.routes import get_owned_conversation
from app.modules.conversations.schemas import ChatIn
from app.schemas import CitationOut
from app.services import rag, semantic_cache
from app.services.chat import (
    ANSWER_STYLES,
    DEFAULT_STYLE,
    STYLE_CONFIG,
    build_chat_model,
    build_prompt,
    resolve_answer_style,
)
from app.services.embedding import embed_query

logger = logging.getLogger(__name__)
router = APIRouter(tags=["qa"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk_answer(text: str, size: int = 50) -> list[str]:
    """缓存命中时按片段模拟流式输出。"""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _load_history(db, conv_id: int, turns: int) -> list[tuple[str, str]]:
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv_id, Message.is_complete.is_(True))
            .order_by(Message.id.desc())
            .limit(turns * 2)
        )
    ).scalars().all()
    msgs = list(reversed(rows))  # 时间正序
    return [(m.role, m.content) for m in msgs if m.role in ("user", "assistant")]


@router.post("/conversations/{conv_id}/chat")
@limiter.limit(settings.chat_rate_limit)
async def chat(
    request: Request,
    conv_id: int,
    body: ChatIn,
    db: DbSession,
    user: CurrentUser,
) -> StreamingResponse:
    conv = await get_owned_conversation(db, conv_id, user.id)

    # 1) 落库用户消息 + 自动标题 + 更新时间
    user_msg = Message(conversation_id=conv.id, role="user", content=body.content, is_complete=True)
    db.add(user_msg)
    conv.last_message_at = _now()
    if conv.title == "新会话" and body.content:
        conv.title = body.content.strip()[:30]
    await db.commit()

    async def gen():
        try:
            async with async_session_factory() as sdb:
                # 2) 检索：问题点名《书名》/「XXX中」→ 限定只搜该文档（BUG-A）
                doc_ids = await rag.resolve_documents_by_title(sdb, body.content)
                # 缓存检索作用域（BUG-B）：选库 kb_id + 点名文档 doc_scope，命中须完全一致
                doc_scope = ",".join(map(str, sorted(doc_ids))) if doc_ids else None
                # 回答风格（单元 F）：会话指定 > 知识库默认；同题不同风格答案不同
                style = await resolve_answer_style(sdb, body.kb_id)
                if body.style in ANSWER_STYLES:
                    style = body.style
                # 风格生成参数：发散风格（拓展延伸/通俗讲解）不缓存 + 高温，重复提问产生多样化回答；
                # 严谨风格保留缓存 + 低温，重复同问给出稳定一致答案。
                style_cfg = STYLE_CONFIG.get(style, STYLE_CONFIG[DEFAULT_STYLE])
                cacheable = bool(style_cfg["cacheable"])
                style_temp = float(style_cfg["temperature"])
                cites = await rag.retrieve(
                    sdb,
                    body.content,
                    kb_id=body.kb_id,
                    doc_ids=doc_ids or None,
                    top_k=settings.top_k_final,
                )
                # 3) 语义缓存（仅严谨风格）：相似、主题一致且**检索作用域一致**的提问直接秒回
                cached = None
                qvec = None
                if cacheable:
                    qvec = await embed_query(body.content)
                    cached = await semantic_cache.find(
                        sdb,
                        qvec,
                        rag.focus_rerank_query(body.content),
                        kb_id=body.kb_id,
                        doc_scope=doc_scope,
                        style=style,
                    )
                if cached:
                    cached_answer, cached_cites = cached
                    yield _sse({"event": "citations", "data": cached_cites})
                    for piece in _chunk_answer(cached_answer):
                        yield _sse({"event": "delta", "data": piece})
                    yield _sse({"event": "done", "data": {"message_id": None, "cached": True}})
                    # 落库缓存答案消息 + 引用行（与真实生成一致，刷新/切会话后数据来源仍在）
                    async with async_session_factory() as ss:
                        asst = Message(
                            conversation_id=conv.id,
                            role="assistant",
                            content=cached_answer,
                            is_complete=True,
                        )
                        ss.add(asst)
                        await ss.flush()
                        for c in cached_cites:
                            ss.add(
                                Citation(
                                    message_id=asst.id,
                                    chunk_id=c.get("chunk_id"),
                                    kb_id=c.get("kb_id"),
                                    doc_id=c.get("doc_id"),
                                    source=c.get("source") or "",
                                    page=c.get("page"),
                                    section=c.get("section"),
                                    snippet=(c.get("snippet") or "")[:1000],
                                    score=c.get("score"),
                                    rank=c.get("rank"),
                                )
                            )
                        conv2 = await ss.get(Conversation, conv.id)
                        if conv2:
                            conv2.last_message_at = _now()
                        await ss.commit()
                    return

                # 4) 历史
                history = await _load_history(sdb, conv.id, settings.history_turns)
                yield _sse({"event": "citations", "data": [c.to_citation().model_dump() for c in cites]})

            # 5) 流式生成（按回答风格组装 SYSTEM_PROMPT + 对应温度）
            llm = build_chat_model(style_temp)
            messages = build_prompt(body.content, cites, history, style=style)
            buffer = ""
            usage_in = usage_out = None
            async for chunk in llm.astream(messages):
                text = chunk.content
                if isinstance(text, list):
                    text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
                if not text:
                    continue
                buffer += text
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    usage_in = usage.get("input_tokens") or usage_in
                    usage_out = usage.get("output_tokens") or usage_out
                yield _sse({"event": "delta", "data": text})

            # 6) 落库助手消息 + 引用
            async with async_session_factory() as sdb:
                asst = Message(
                    conversation_id=conv.id,
                    role="assistant",
                    content=buffer,
                    is_complete=True,
                    usage_input_tokens=usage_in,
                    usage_output_tokens=usage_out,
                )
                sdb.add(asst)
                await sdb.flush()
                cite_dicts = []
                for c in cites:
                    cite_dicts.append(c.to_citation().model_dump())
                    sdb.add(
                        Citation(
                            message_id=asst.id,
                            chunk_id=c.chunk_id,
                            kb_id=c.kb_id,
                            doc_id=c.doc_id,
                            source=c.source,
                            page=c.page,
                            section=c.section,
                            snippet=c.snippet[:1000],
                            score=c.score,
                            rank=c.rank,
                        )
                    )
                conv2 = await sdb.get(Conversation, conv.id)
                if conv2:
                    conv2.last_message_at = _now()
                await sdb.commit()
                asst_id = asst.id
            # 7) 写入语义缓存（仅严谨风格；发散风格不缓存，保证每次重新生成出变化）
            if cacheable:
                try:
                    async with async_session_factory() as sdb:
                        await semantic_cache.store(
                            sdb,
                            qvec,
                            rag.focus_rerank_query(body.content),
                            buffer,
                            cite_dicts,
                            kb_id=body.kb_id,
                            doc_scope=doc_scope,
                            style=style,
                        )
                except Exception:
                    logger.debug("语义缓存写入失败，忽略")
            yield _sse({"event": "done", "data": {"message_id": asst_id}})

        except Exception as exc:
            logger.exception("问答流异常 conv=%s", conv.id)
            yield _sse({"event": "error", "data": _user_friendly_error(exc)})
            # 落库失败消息（供历史展示）
            try:
                async with async_session_factory() as sdb:
                    sdb.add(
                        Message(
                            conversation_id=conv.id,
                            role="assistant",
                            content="回答生成失败，请稍后重试。",
                            is_complete=False,
                            error=str(exc)[:1000],
                        )
                    )
                    await sdb.commit()
            except Exception:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _user_friendly_error(exc: Exception) -> str:
    """把底层异常转为用户可读提示（不泄露敏感信息）。"""
    msg = str(exc)
    if "AuthenticationError" in msg or "401" in msg or "Invalid API key" in msg:
        return "LLM 或 Embedding 的 API Key 无效，请在 .env 中检查配置"
    if "rate" in msg.lower() or "429" in msg or "too many" in msg.lower():
        return "服务请求过于频繁（限流），请稍后重试"
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return "服务响应超时，请稍后重试"
    if settings.debug:
        return msg
    return "生成失败，请稍后重试"
