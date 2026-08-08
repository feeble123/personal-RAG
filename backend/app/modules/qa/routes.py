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
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.db.models import Citation, Conversation, Message
from app.db.session import async_session_factory
from app.modules.conversations.routes import get_owned_conversation
from app.modules.conversations.schemas import ChatIn
from app.schemas import CitationOut
from app.services import intent, memory, query_rewrite, rag, semantic_cache
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
                # 追问改写（BUG-追问引用错位）：短/指代性追问（「可以以表格形式呈现吗」等）
                # 合并上一轮「主题问题」，让新问题自行检索到同主题切片，避免模型抄历史答案+沿用历史编号。
                # 注意要跳过本身是追问的问题（如「可以以表格吗」「6-11呢」），回退找最近一条主题问题。
                prev_user_q = None
                prev_rows = (
                    await sdb.execute(
                        select(Message.content)
                        .where(
                            Message.conversation_id == conv.id,
                            Message.role == "user",
                            Message.id < user_msg.id,
                        )
                        .order_by(Message.id.desc())
                        .limit(6)
                    )
                ).all()
                for (content,) in prev_rows:
                    if content and not query_rewrite.needs_followup_rewrite(content):
                        prev_user_q = content
                        break
                if prev_user_q is None and prev_rows:
                    prev_user_q = prev_rows[0][0]  # 全为追问时退化到最近一条
                search_query = query_rewrite.rewrite_followup_query(body.content, prev_user_q)
                # 2) 检索：问题点名《书名》/「XXX中」→ 限定只搜该文档（BUG-A）
                doc_ids = await rag.resolve_documents_by_title(sdb, search_query)
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
                # 3) 问答记忆（用户背书，先于检索）：命中即秒回，不浪费检索
                #    good → 直接复用记忆答案；bad → 强制跳过语义缓存重新检索
                subject = rag.focus_rerank_query(body.content)
                skip_cache = False
                qvec = None
                if settings.memory_enabled:
                    qvec = await embed_query(body.content)
                    mem = await memory.recall(
                        sdb, qvec, subject,
                        user_id=user.id, kb_id=body.kb_id, doc_scope=doc_scope, style=style,
                    )
                    if mem is not None and mem.status == "good":
                        # 命中复用：先落库拿真实 message_id，再发 done（带 from_memory 标记）
                        yield _sse({"event": "citations", "data": mem.citations})
                        for piece in _chunk_answer(mem.answer):
                            yield _sse({"event": "delta", "data": piece})
                        async with async_session_factory() as ss:
                            asst = Message(
                                conversation_id=conv.id,
                                role="assistant",
                                content=mem.answer,
                                is_complete=True,
                                from_memory=True,
                                kb_id=body.kb_id,
                                doc_scope=doc_scope,
                                style=style,
                            )
                            ss.add(asst)
                            await ss.flush()
                            for c in mem.citations:
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
                            asst_id = asst.id
                        yield _sse(
                            {"event": "done",
                             "data": {"message_id": asst_id, "cached": True, "from_memory": True}}
                        )
                        return
                    if mem is not None and mem.status == "bad":
                        skip_cache = True
                        logger.info("负面记忆命中 mem=%s，强制重新检索", mem.memory_id)

                # 4) 检索（记忆未命中才执行）：用改写后的查询检索（追问合并主题，保证新问题自行检索）
                cites = await rag.retrieve(
                    sdb,
                    search_query,
                    kb_id=body.kb_id,
                    doc_ids=doc_ids or None,
                    top_k=settings.top_k_final,
                )
                # U3 证据等级：按检索分数四级判级
                scores = [c.score for c in cites if c.score is not None]
                evidence_level = rag.judge_evidence_level(scores)
                evidence_top_score = scores[0] if scores else None
                # U3 动态放行：仅当「实时/外部信息」类问题（天气/时间/新闻/汇率等，系统无此能力）
                # 且 KB 无强证据时拒答；问候/闲聊/能力咨询/规范概述/领域问答一律放行，
                # 由 LLM 依据证据等级诚实作答（规则8 + 较弱约束，不强行引用弱相关资料）。
                # 核心原则：知识库有内容（partial/sufficient）就必须放行。
                if evidence_level in ("none", "weak") and intent.is_real_time_query(body.content):
                    refusal = (
                        "抱歉，这类问题需要实时或外部信息（如天气、时间、最新动态），"
                        "我目前不具备联网与实时数据获取能力。"
                        "我是水利工程知识库问答助手，可解答水利工程相关的规范条文、设计施工、防汛调度等专业问题。"
                    )
                    yield _sse({"event": "citations", "data": []})
                    for piece in _chunk_answer(refusal):
                        yield _sse({"event": "delta", "data": piece})
                    async with async_session_factory() as ss:
                        asst = Message(
                            conversation_id=conv.id,
                            role="assistant",
                            content=refusal,
                            is_complete=True,
                            from_memory=False,
                            kb_id=body.kb_id,
                            doc_scope=doc_scope,
                            style=style,
                            evidence_level=evidence_level,
                            evidence_top_score=evidence_top_score,
                        )
                        ss.add(asst)
                        conv2 = await ss.get(Conversation, conv.id)
                        if conv2:
                            conv2.last_message_at = _now()
                        await ss.commit()
                        asst_id = asst.id
                    yield _sse(
                        {"event": "done",
                         "data": {"message_id": asst_id, "evidence_level": evidence_level,
                                  "evidence_top_score": evidence_top_score}}
                    )
                    return
                # 5) 语义缓存（仅严谨风格；负面记忆命中时跳过）：相似、主题一致且**作用域一致**直接秒回
                cached = None
                if cacheable and not skip_cache:
                    if qvec is None:
                        qvec = await embed_query(body.content)
                    cached = await semantic_cache.find(
                        sdb,
                        qvec,
                        subject,
                        kb_id=body.kb_id,
                        doc_scope=doc_scope,
                        style=style,
                    )
                if cached:
                    cached_answer, cached_cites = cached
                    yield _sse({"event": "citations", "data": cached_cites})
                    for piece in _chunk_answer(cached_answer):
                        yield _sse({"event": "delta", "data": piece})
                    # 落库缓存答案消息 + 引用行（先落库拿真实 message_id，缓存重放也能点赞/踩）
                    async with async_session_factory() as ss:
                        asst = Message(
                            conversation_id=conv.id,
                            role="assistant",
                            content=cached_answer,
                            is_complete=True,
                            from_memory=False,
                            kb_id=body.kb_id,
                            doc_scope=doc_scope,
                            style=style,
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
                        asst_id = asst.id
                    yield _sse(
                        {"event": "done",
                         "data": {"message_id": asst_id, "cached": True, "from_memory": False}}
                    )
                    return

                # 6) 历史
                history = await _load_history(sdb, conv.id, settings.history_turns)
                yield _sse({"event": "citations", "data": [c.to_citation().model_dump() for c in cites]})

            # 5) 流式生成（按回答风格组装 SYSTEM_PROMPT + 对应温度；证据较弱/不足时追加据实约束）
            llm = build_chat_model(style_temp)
            messages = build_prompt(
                body.content, cites, history, style=style, evidence_weak=(evidence_level in ("weak", "none"))
            )
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
                    kb_id=body.kb_id,
                    doc_scope=doc_scope,
                    style=style,
                    evidence_level=evidence_level,
                    evidence_top_score=evidence_top_score,
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
                            subject,
                            buffer,
                            cite_dicts,
                            kb_id=body.kb_id,
                            doc_scope=doc_scope,
                            style=style,
                        )
                except Exception:
                    logger.debug("语义缓存写入失败，忽略")
            yield _sse(
                {"event": "done",
                 "data": {"message_id": asst_id, "evidence_level": evidence_level,
                          "evidence_top_score": evidence_top_score}}
            )

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


class FeedbackIn(BaseModel):
    """消息反馈：up=点赞(沉淀正向记忆) / down=点踩(沉淀负面记忆) / null=取消评价。"""

    feedback: Literal["up", "down"] | None = None


def _memory_config() -> memory.MemoryConfig:
    return memory.MemoryConfig(
        enabled=settings.memory_enabled,
        threshold=settings.memory_threshold,
        max_entries=settings.memory_max_entries,
        pool=settings.memory_pool,
        eviction_ratio=settings.memory_eviction_ratio,
    )


@router.post("/conversations/{conv_id}/messages/{message_id}/feedback")
@limiter.limit(settings.feedback_rate_limit)
async def message_feedback(
    request: Request,
    conv_id: int,
    message_id: int,
    body: FeedbackIn,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """记录消息反馈：up → 沉淀正向记忆；down → 沉淀负面记忆；null → 取消评价（不动记忆）。"""
    conv = await get_owned_conversation(db, conv_id, user.id)
    asst = await db.get(Message, message_id)
    if asst is None or asst.conversation_id != conv.id or asst.role != "assistant" or not asst.is_complete:
        raise BizError("消息不存在", 404, "MSG_NOT_FOUND")
    # 前置最近的 user 问题（记忆的 question）
    prev = await db.scalar(
        select(Message)
        .where(Message.conversation_id == conv.id, Message.role == "user", Message.id < asst.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    if prev is None:
        raise BizError("找不到该回答对应的问题", 400, "NO_PREV_USER_MSG")

    if body.feedback in ("up", "down"):
        asst.feedback = body.feedback
        await db.commit()
        # 沉淀记忆（尽力而为，失败不回滚已记录的反馈）
        try:
            qvec = await embed_query(prev.content)
            cites = (
                await db.execute(
                    select(Citation).where(Citation.message_id == asst.id).order_by(Citation.rank)
                )
            ).scalars().all()
            cite_dicts = [CitationOut.model_validate(c).model_dump() for c in cites]
            await memory.record_feedback(
                db,
                user_id=user.id,
                question=prev.content,
                answer=asst.content,
                citations=cite_dicts,
                feedback=body.feedback,
                query_vector=qvec,
                subject=rag.focus_rerank_query(prev.content),
                kb_id=asst.kb_id,
                doc_scope=asst.doc_scope,
                style=asst.style,
                config=_memory_config(),
            )
        except Exception:
            logger.exception("记忆沉淀失败（反馈已记录）")
        return {"feedback": body.feedback}

    # 取消评价
    asst.feedback = None
    await db.commit()
    return {"feedback": None}


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
