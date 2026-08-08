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

# 基础 Prompt：所有风格共享的 RAG 底线（引用、完整性、不编造、公式、中文）
BASE_PROMPT = """你是一名专业的水利工程知识库问答助手。你的任务是基于提供的【参考资料】准确、完整地回答用户关于水利工程的问题。

通用要求（所有风格都必须遵守）：
1. 必须综合【参考资料】中与问题相关的全部内容作答，逐条呈现，不得因引用条目较多而省略任何已有要点；每点标注引用编号，如 [1]、[2]。
2. 若问题涉及多个方面（如"包括哪些""有哪些要求""是什么"），应把参考资料中能回答该问题的所有条目都整理并展开，覆盖完整。
3. 只有当参考资料确实不包含某方面内容时，才说明"参考资料未覆盖该内容"；绝不把参考资料中已有的内容说成未覆盖或缺文档。
4. 引用中明确给出的原文要点应完整转述，不要自行概括成一句带过。
5. 公式用 LaTeX 排版：行内公式用 `$...$`，独立公式用 `$$...$$`（如 `$$v = C\sqrt{Ri}$$`），确保前端数学排版渲染。
6. 使用中文回答；回答末尾不额外补充与问题无关的内容。
7. 若【对话历史】中出现过与本问题相同或相似的问题，必须按当前【回答风格】重新组织语言作答——换表述角度、换例子/比喻，不得照搬历史回答原文；回答的内容要点仍须与【参考资料】一致、保持准确。
8. 若【参考资料】与【当前问题】明显无关（如问候、闲聊、寒暄，或问题明显不属于水利工程知识库范围），**不得**强行引用参考资料作答、更不得把资料内容当作该问题的答案——应直接简短自然地回应（如问候回礼），或礼貌说明该问题不在知识库覆盖范围内，请用户咨询水利工程相关问题。
9. **引用编号纪律**：引用编号 [n] 必须精确对应当前【参考资料】中的条目（[1] 即第 1 条资料），只能标注当前参考资料中确实存在的编号。若某个内容或结论的依据来自【对话历史】或你的通用知识，而非当前【参考资料】，**不得**标注引用编号，应如实说明该内容未在当前参考资料中找到依据。**严禁**沿用、跳过或臆造历史对话中出现过的编号——每一轮问答的【参考资料】编号彼此独立，绝不与历史编号混淆。"""

# 回答风格（单元 F）：知识库可选，问答时按风格叠加专属指令
ANSWER_STYLES: dict[str, str] = {
    "standard": """【回答风格：规范条文式】（严谨·标准）
- 以规范条文口吻作答：先一句话总述，再逐条列出要点，编号与引用对齐（如「①……[1]；②……[2]」）。
- 术语精确、直接陈述；不解释"为什么"，不做延伸拓展，不掺主观建议。""",
    "logical": """【回答风格：专业论证式】（严谨·逻辑）
- 先给结论（一句话），再按逻辑链展开依据与推理（因为…所以…，首先…其次…最后…）。
- 解释概念机理、因果联系，适合"为什么""如何推导"类问题；关键推理步骤标注引用。""",
    "summary": """【回答风格：要点摘要式】（严谨·高效）
- 第一段用 2~3 句话总括核心结论。
- 随后分点列出关键要点，每条一句话、简洁有力，末尾标注引用编号。
- 不做长篇展开，便于快速查阅。""",
    "expanded": """【回答风格：拓展延伸式】（发散）
- 先完整回答核心问题（含引用），再补充相关背景、上下游环节、实际工程应用场景、常见误区或注意事项。
- 拓展内容必须是参考资料已提及的信息或公认常识，不得虚构具体数据/条文；无法确认的拓展明确说明"（资料未提及，以下为一般了解）"。
- 发散性风格：若此前已回答过同类问题，请主动更换展开角度、补充不同的应用场景或注意点，避免与上次答案雷同。""",
    "tutorial": """【回答风格：通俗讲解式】（趣味）
- 面向非专业读者：先用生活化比喻或具体例子建立直观印象，再点出专业术语与规范要点（标注引用）。
- 由浅入深、循序渐进；避免堆砌术语，必要时给术语加一句通俗解释。
- 发散性风格：若此前已回答过同类问题，请更换一个不同的比喻或例子重新讲解（例如上次用"做菜"，这次改用盖房子、搭积木等），用不同的直观方式组织语言。""",
}

# 每风格的生成参数：temperature（温度越高越多样）+ cacheable（是否允许语义缓存重放）。
# 严谨风格（规范条文/要点摘要/专业论证）：重复相同问题给出一致答案更合理 → 可缓存、低温。
# 发散风格（拓展延伸/通俗讲解）：重复提问应产生不同的例子/角度 → 不缓存、高温，每次重新生成。
STYLE_CONFIG: dict[str, dict[str, object]] = {
    "standard": {"temperature": 0.2, "cacheable": True},
    "logical": {"temperature": 0.2, "cacheable": True},
    "summary": {"temperature": 0.2, "cacheable": True},
    "expanded": {"temperature": 0.9, "cacheable": False},
    "tutorial": {"temperature": 0.9, "cacheable": False},
}

STYLE_LABELS: dict[str, str] = {
    "standard": "规范条文式",
    "logical": "专业论证式",
    "summary": "要点摘要式",
    "expanded": "拓展延伸式",
    "tutorial": "通俗讲解式",
}

DEFAULT_STYLE = "standard"

# 按 temperature 缓存模型实例（发散风格用高温，严谨风格用低温）
_models: dict[float, object] = {}


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


def build_chat_model(temperature: float | None = None):
    """返回 LLM 实例，按 temperature 缓存。LLM_PROVIDER=fake 时返回模拟模型（离线）。

    temperature：覆盖全局 llm_temperature（发散风格用高温以产生多样化回答）。
    """
    temp = settings.llm_temperature if temperature is None else float(temperature)
    if temp in _models:
        return _models[temp]
    if settings.llm_provider == "fake":
        _models[temp] = _FakeLLM()
        logger.warning("使用 FAKE LLM（离线演示模式），回答为模拟内容")
        return _models[temp]
    if not settings.deepseek_api_key:
        raise BizError("未配置 DEEPSEEK_API_KEY（https://platform.deepseek.com 获取）", 500, "LLM_NOT_CONFIGURED")
    from langchain_deepseek import ChatDeepSeek

    _models[temp] = ChatDeepSeek(
        model=settings.llm_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temp,
        max_tokens=settings.llm_max_tokens,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout,
    )
    logger.info("LLM 客户端就绪: %s @ %s (temperature=%s)", settings.llm_model, settings.deepseek_base_url, temp)
    return _models[temp]


def reset_chat_model() -> None:
    _models.clear()


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


async def resolve_answer_style(db, kb_id: int | None) -> str:
    """解析问答回答风格：优先会话指定（调用方已处理），否则取知识库默认，最后回退默认风格。"""
    if kb_id:
        from app.db.models import KnowledgeBase

        kb = await db.get(KnowledgeBase, kb_id)
        if kb and kb.answer_style in ANSWER_STYLES:
            return kb.answer_style
    return DEFAULT_STYLE


def build_prompt(
    query: str,
    cites: list[RetrievedChunk],
    history: list[tuple[str, str]] | None = None,
    style: str = DEFAULT_STYLE,
    evidence_weak: bool = False,
) -> list[tuple[str, str]]:
    """组装消息列表：[system, (history...), human]。返回 ChatMessages 输入。

    style：回答风格 key（standard/logical/summary/expanded/tutorial），
    系统提示 = 基础 RAG 底线 + 风格专属指令。
    evidence_weak：检索证据等级为「较弱」时追加约束——据实作答、不编造、不强行凑数。
    """
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

    style_block = ANSWER_STYLES.get(style, ANSWER_STYLES[DEFAULT_STYLE])
    if evidence_weak:
        # U3：证据等级「较弱」时追加约束，防止强行引用弱相关资料凑数
        style_block += (
            "\n【证据提示】本次检索到的参考资料相关性较弱，可能与问题只是部分相关。"
            "请据实作答：能依据资料回答的部分明确回答；资料不足以支撑的部分，"
            "如实说明「知识库资料未覆盖该内容」，不得编造、不得强行引用弱相关资料凑数。"
        )
    system = BASE_PROMPT + "\n\n" + style_block
    messages: list[tuple[str, str]] = [("system", system)]
    if history:
        for role, text in history:
            messages.append((role, text[:1000]))
    messages.append(("human", "\n\n".join(parts)))
    return messages
