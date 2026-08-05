"""LLM 聊天服务：ChatDeepSeek 单例 + prompt 组装 + 多轮历史注入。

升级路径：统一 `build_chat_model()` 工厂，`LLM_PROVIDER` 可切 ChatOpenAI/Qwen 等。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from app.core.config import settings
from app.core.exceptions import BizError
from app.services.rag import RetrievedChunk

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一名专业的水利工程知识库问答助手。你的任务是基于提供的【参考资料】准确、完整地回答用户关于水利工程的问题。

要求：
1. 必须综合【参考资料】中与问题相关的全部内容作答，逐条呈现，不得因引用条目较多而省略任何已有的要点；每点标注引用编号，如 [1]、[2]。
2. 若问题涉及多个方面（如"包括哪些""有哪些要求""是什么"），应把参考资料中能回答该问题的所有条目都整理并展开，覆盖完整。
3. 只有当参考资料确实不包含某方面内容时，才说明"参考资料未覆盖该内容"；绝不把参考资料中已有的内容说成未覆盖或缺文档。
4. 引用中明确给出的原文要点应完整转述，不要自行概括成一句带过。
5. 使用中文回答，条理清晰、简洁准确。
6. 回答末尾不额外补充与问题无关的内容。"""

_model = None


class _FakeLLM:
    """离线测试/演示模式（LLM_PROVIDER=fake）：模拟流式输出，无需 API Key。

    引用检索到的最相关内容，便于离线验证整条问答 + 引用链路。
    """

    async def astream(self, messages):  # noqa: ANN001
        parts = [
            "根据知识库参考资料，",
            "明渠均匀流是指水流沿程流速、水深和断面保持不变的水流。",
            "其形成条件包括：长直棱柱体渠道、正坡、糙率沿程不变、无汇入分出。",
            "（离线演示回复，配置 DEEPSEEK_API_KEY 后可获得真实回答）",
        ]
        for p in parts:
            yield SimpleNamespace(content=p, usage_metadata=None)


def build_chat_model():
    """返回 LLM 单例。LLM_PROVIDER=fake 时返回模拟模型（离线）。"""
    global _model
    if _model is not None:
        return _model
    if settings.llm_provider == "fake":
        _model = _FakeLLM()
        logger.warning("使用 FAKE LLM（离线演示模式），回答为模拟内容")
        return _model
    if not settings.deepseek_api_key:
        raise BizError("未配置 DEEPSEEK_API_KEY（https://platform.deepseek.com 获取）", 500, "LLM_NOT_CONFIGURED")
    from langchain_deepseek import ChatDeepSeek

    _model = ChatDeepSeek(
        model=settings.llm_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout,
    )
    logger.info("LLM 客户端就绪: %s @ %s", settings.llm_model, settings.deepseek_base_url)
    return _model


def reset_chat_model() -> None:
    global _model
    _model = None


def _format_citations(cites: list[RetrievedChunk]) -> str:
    parts = []
    for i, c in enumerate(cites, start=1):
        loc = c.source
        if c.page:
            loc += f"，第{c.page}页"
        if c.section:
            loc += f"，{c.section}"
        parts.append(f"[{i}] 来源：{loc}\n{c.snippet}")
    return "\n\n".join(parts)


def build_prompt(
    query: str,
    cites: list[RetrievedChunk],
    history: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """组装消息列表：[system, (history...), human]。返回 ChatMessages 输入。"""
    if not cites:
        ref_section = "（没有检索到相关参考资料）"
    else:
        ref_section = _format_citations(cites)

    parts = ["【参考资料】", ref_section]

    if history:
        hist_lines = ["【对话历史】"]
        for role, text in history:
            prefix = "用户" if role == "user" else "助手"
            hist_lines.append(f"{prefix}：{text[:500]}")
        parts.append("\n".join(hist_lines))

    parts.append(f"【当前问题】\n{query}")

    messages: list[tuple[str, str]] = [("system", SYSTEM_PROMPT)]
    if history:
        for role, text in history:
            messages.append((role, text[:1000]))
    messages.append(("human", "\n\n".join(parts)))
    return messages
