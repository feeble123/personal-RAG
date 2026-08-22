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
from app.db.models import Chunk, Citation, Conversation, Message
from app.db.session import async_session_factory
from app.modules.conversations.routes import get_owned_conversation
from app.modules.conversations.schemas import ChatIn
from app.schemas import CitationOut
from app.services import intent, memory, query_rewrite, rag, semantic_cache, verify
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


async def _existing_chunk_ids(db, chunk_ids: list) -> set[int]:
    """批量校验 chunk 是否仍存在（P0-5）。

    重灌/删文档后，记忆（QaMemory）与语义缓存里存的旧 chunk_id 可能已指向已删行——
    重放引用时若直接写非空 chunk_id 会外键违约。校验后只保留真实存在的，
    缺失的置 NULL（快照字段 source/page/section/snippet/doc_id 仍可显示）。
    """
    ids = {int(x) for x in chunk_ids if x is not None}
    if not ids:
        return set()
    found = (await db.scalars(select(Chunk.id).where(Chunk.id.in_(ids)))).all()
    return set(found)


def _chunk_answer(text: str, size: int = 50) -> list[str]:
    """缓存命中时按片段模拟流式输出。"""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _finish_reason(chunk) -> str | None:
    """从流式 chunk 里取 finish_reason（== 'length' 表示生成因超出 max_tokens 被截断）。"""
    meta = getattr(chunk, "response_metadata", None)
    if isinstance(meta, dict) and meta.get("finish_reason"):
        return str(meta["finish_reason"])
    gi = getattr(chunk, "generation_info", None)
    if isinstance(gi, dict) and gi.get("finish_reason"):
        return str(gi["finish_reason"])
    fr = getattr(chunk, "finish_reason", None)
    return str(fr) if fr else None


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
    out: list[tuple[str, str]] = []
    for m in msgs:
        if m.role not in ("user", "assistant"):
            continue
        text = (m.content or "").strip()
        # 长回答（表格/长清单）只保留开头概要，防止历史注水稀释当前证据（长对话变笨）
        if m.role == "assistant" and len(text) > 500:
            text = text[:400] + "\n…[该回答较长，历史注入仅保留开头概要]"
        out.append((m.role, text))
    return out


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
                # P0-2 scope 隔离：书名解析限定当前库（KB-A 点名 KB-B 同名文档 → 解析为空，不跨库）
                doc_ids = await rag.resolve_documents_by_title(sdb, search_query, kb_id=body.kb_id)
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
                            # P0-5：重灌后记忆里的旧 chunk_id 可能已删，校验后缺失置 NULL（快照仍可显示）
                            valid_ids = await _existing_chunk_ids(
                                ss, [c.get("chunk_id") for c in mem.citations]
                            )
                            for c in mem.citations:
                                ss.add(
                                    Citation(
                                        message_id=asst.id,
                                        chunk_id=c.get("chunk_id") if c.get("chunk_id") in valid_ids else None,
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
                        "抱歉，这类问题需要实时或外部信息（如天气、时间、最新动态、实时水位与水情等），"
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
                        user_id=user.id,  # P0-3 缓存按用户隔离
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
                        # P0-5：重灌后缓存里的旧 chunk_id 可能已删，校验后缺失置 NULL（快照仍可显示）
                        valid_ids = await _existing_chunk_ids(
                            ss, [c.get("chunk_id") for c in cached_cites]
                        )
                        for c in cached_cites:
                            ss.add(
                                Citation(
                                    message_id=asst.id,
                                    chunk_id=c.get("chunk_id") if c.get("chunk_id") in valid_ids else None,
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
            finish_reason = None
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
                fr = _finish_reason(chunk)
                if fr:
                    finish_reason = fr
                yield _sse({"event": "delta", "data": text})

            # 5.5) 完备性校验（opt-in，默认关；用户不满意可点「🤖 LLM优化」触发 /optimize）。
            #      硬信号兜底：生成因超出 max_tokens 被截断（finish_reason=length）→ 诚实标记不完整。
            truncated = finish_reason == "length"
            answer_complete = False if truncated else None
            if settings.answer_verify_enabled and buffer and cites:
                verdict = await verify.verify_completeness(body.content, buffer, cites)
                if verdict.enumeration and not verdict.complete:
                    async with async_session_factory() as vsdb:
                        broader = await rag.retrieve_document_wide(
                            vsdb, search_query, kb_id=body.kb_id, top_k=settings.top_k_final
                        )
                    if broader:
                        logger.info("完备性校验不通过(note=%s)，扩大证据重生成", verdict.note[:80])
                        yield _sse({"event": "reset"})  # 前端清空本回答，重新流式
                        yield _sse(
                            {"event": "citations",
                             "data": [c.to_citation().model_dump() for c in broader]}
                        )
                        llm2 = build_chat_model(style_temp)
                        messages2 = build_prompt(
                            body.content, broader, history=None, style=style, note_incomplete=True
                        )
                        buffer = ""
                        async for chunk in llm2.astream(messages2):
                            text = chunk.content
                            if isinstance(text, list):
                                text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
                            if not text:
                                continue
                            buffer += text
                            yield _sse({"event": "delta", "data": text})
                        cites = broader
                    answer_complete = False
                elif verdict.enumeration:
                    answer_complete = True

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
                    answer_complete=answer_complete,
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
            # 7) 写入语义缓存（仅严谨风格；发散风格不缓存；被截断的坏答案不缓存，
            #    否则长对话里近似问法会秒回截断坏答案——「越问越笨」的根因之一）
            if cacheable and not truncated:
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
                            user_id=user.id,  # P0-3 缓存按用户隔离
                        )
                except Exception:
                    logger.debug("语义缓存写入失败，忽略")
            yield _sse(
                {"event": "done",
                 "data": {"message_id": asst_id, "evidence_level": evidence_level,
                          "evidence_top_score": evidence_top_score,
                          "answer_complete": answer_complete}}
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


@router.post("/conversations/{conv_id}/messages/{message_id}/optimize")
@limiter.limit(settings.chat_rate_limit)
async def optimize_message(
    request: Request,
    conv_id: int,
    message_id: int,
    db: DbSession,
    user: CurrentUser,
) -> StreamingResponse:
    """LLM 优化（opt-in）：用户对回答不满意时触发。

    用该回答对应的问题重新走「整文档扩展证据 + 补全要求重生成 + 完备性校验循环」，
    落库为**新的** assistant 消息（原回答保留可对比），SSE 流式返回。
    事件协议同 /chat：citations → delta（→ reset+delta 重试）→ done / error。
    """
    conv = await get_owned_conversation(db, conv_id, user.id)
    asst = await db.get(Message, message_id)
    if asst is None or asst.conversation_id != conv.id or asst.role != "assistant" or not asst.is_complete:
        raise BizError("消息不存在", 404, "MSG_NOT_FOUND")
    prev = await db.scalar(
        select(Message)
        .where(Message.conversation_id == conv.id, Message.role == "user", Message.id < asst.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    if prev is None:
        raise BizError("找不到该回答对应的问题", 400, "NO_PREV_USER_MSG")

    # 问题可能是追问（「只要方案的，不要制度的」）→ 合并最近主题问题，让新问题自行检索同主题切片
    question = prev.content
    topic = question
    for (content,) in (
        await db.execute(
            select(Message.content)
            .where(Message.conversation_id == conv.id, Message.role == "user", Message.id < prev.id)
            .order_by(Message.id.desc())
            .limit(6)
        )
    ).all():
        if content and not query_rewrite.needs_followup_rewrite(content):
            topic = content
            break
    search_query = query_rewrite.rewrite_followup_query(question, topic if topic != question else None)
    kb_id = asst.kb_id
    style = asst.style or DEFAULT_STYLE

    async def gen():
        try:
            async with async_session_factory() as sdb:
                # 先普通检索拿证据等级（供新消息打标），再整文档扩展证据（复用已检索结果避免二次检索）
                cites = await rag.retrieve(sdb, search_query, kb_id=kb_id, top_k=settings.top_k_final)
                scores = [c.score for c in cites if c.score is not None]
                evidence_level = rag.judge_evidence_level(scores)
                evidence_top_score = scores[0] if scores else None
                broader = await rag.retrieve_document_wide(
                    sdb, search_query, kb_id=kb_id, top_k=settings.top_k_final, _cites=cites
                )
                if not broader:
                    yield _sse({"event": "error", "data": "未能检索到相关资料，无法优化。"})
                    return
                cites = broader
                yield _sse({"event": "citations", "data": [c.to_citation().model_dump() for c in cites]})

            style_cfg = STYLE_CONFIG.get(style, STYLE_CONFIG[DEFAULT_STYLE])
            style_temp = float(style_cfg["temperature"])
            buffer = ""
            verdict = verify.CompletenessVerdict()
            truncated = False
            incomplete = True
            attempts = 0
            while incomplete and attempts < max(1, settings.answer_verify_max_retries):
                attempts += 1
                llm = build_chat_model(style_temp)
                messages = build_prompt(
                    question, cites, history=None, style=style, note_incomplete=(attempts > 1)
                )
                buffer = ""
                truncated = False
                async for chunk in llm.astream(messages):
                    text = chunk.content
                    if isinstance(text, list):
                        text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
                    if not text:
                        continue
                    buffer += text
                    if _finish_reason(chunk) == "length":
                        truncated = True
                    yield _sse({"event": "delta", "data": text})
                # 完备性校验：枚举题遗漏 / 输出被截断 → 再带「补全要求」重生成
                verdict = await verify.verify_completeness(question, buffer, cites)
                incomplete = (verdict.enumeration and not verdict.complete) or truncated
                if incomplete and attempts < max(1, settings.answer_verify_max_retries):
                    yield _sse({"event": "reset"})  # 前端清空本次优化气泡，重新流式
                    yield _sse(
                        {"event": "citations", "data": [c.to_citation().model_dump() for c in cites]}
                    )
            # 枚举题/被截断 → 按最终校验结果打标；非枚举题不适用完备性
            answer_complete = (not incomplete) if (verdict.enumeration or truncated) else None
            # 落库新消息（原回答保留对比）
            async with async_session_factory() as sdb:
                new_asst = Message(
                    conversation_id=conv.id,
                    role="assistant",
                    content=buffer,
                    is_complete=True,
                    kb_id=kb_id,
                    doc_scope=asst.doc_scope,
                    style=style,
                    evidence_level=evidence_level,
                    evidence_top_score=evidence_top_score,
                    answer_complete=answer_complete,
                    is_optimized=True,
                )
                sdb.add(new_asst)
                await sdb.flush()
                for c in cites:
                    sdb.add(
                        Citation(
                            message_id=new_asst.id,
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
                new_id = new_asst.id
            yield _sse(
                {"event": "done",
                 "data": {"message_id": new_id, "optimized": True,
                          "answer_complete": answer_complete,
                          "evidence_level": evidence_level,
                          "evidence_top_score": evidence_top_score}}
            )
        except Exception as exc:
            logger.exception("LLM优化流异常 conv=%s msg=%s", conv.id, message_id)
            yield _sse({"event": "error", "data": _user_friendly_error(exc)})

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
