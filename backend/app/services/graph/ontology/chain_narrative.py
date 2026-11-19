"""把链路骨架写成一段通顺的话（C 方案的 B 步）。

**边界很硬**：事实、顺序、出处都由 `chain.py` 定死，模型只做措辞。
所以这里做三件事：

1. 只把骨架喂给模型，不给它任何额外背景（不给它会"发挥"的机会）；
2. 生成后**校验数字**：正文里出现的每个数值都必须在骨架里出现过，否则整段丢弃——
   编出一个"下泄 3500 m³/s"比没有正文糟得多；
3. 任何失败（模型不可用、超时、校验不过）都只降级为"只有骨架"，不影响问答主链路。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 数值白名单校验用；百分比、小数、千分位都要能认出来
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: 低温，尽量少发挥
TEMPERATURE = 0.2

SYSTEM_PROMPT = """你是水利防汛知识助手。请把给定的"链路事实"整理成一段说明文。

硬性要求（违反任何一条都算失败）：
1. 只能使用给定事实里的信息，**不得引入新的数值、名称、地名、时间或建议**；
2. 严格按给定顺序叙述，不要调整步骤先后；同一主语的多条并列事实可以合并成一句
   （如"数据准备执行了 A、B、C 三项"），但不得省略、不得改变先后；
3. 有出处的在句末标注【文档名 p.页码】；没有出处的**不要逐条声明**——
   只有在提示里明确说有"无出处环节"时，才在最后补一句
   "以上未标注出处的环节，原文未提供依据"；全部有出处时**不要**加这句话；
4. 写 3~8 句说明文，不要用项目符号，不要逐条罗列，不要写"根据您的问题"这类客套话。"""


class NarrativeRejected(Exception):
    """模型输出里出现了骨架中没有的数值。"""


def _fmt_where(evidence: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in evidence or []:
        name = str(item.get("doc_name") or "").strip()
        page = item.get("printed_page") or item.get("page")
        if name and page:
            parts.append(f"{name} p.{page}")
        elif name:
            parts.append(name)
        elif page:
            parts.append(f"p.{page}")
    return "；".join(parts)


def build_user_prompt(question: str, chain: dict[str, Any]) -> str:
    """把骨架压成清单。只放事实，不放原始检索片段——片段里的干扰信息越少越好。"""
    lines = [f"问题：{question}", "", "链路起点：" + str(chain.get("subject") or "（未识别）")]
    if chain.get("conditions"):
        lines.append("工况：" + "、".join(chain["conditions"]))
    lines.append("")
    lines.append("链路事实（唯一依据，按顺序）：")
    for index, step in enumerate(chain.get("steps") or [], start=1):
        where = _fmt_where(step.get("evidence") or []) or "无出处"
        props = step.get("object_props") or {}
        # `*_count` 是图的派生字段（证据条数、范围条数），不是事实本身，别喂给模型
        extra = "，".join(
            f"{k}={v}"
            for k, v in props.items()
            if v not in (None, "") and not k.endswith("_count") and k != "display_zh"
        )
        lines.append(
            f"{index}. [{step.get('phase_label')}] "
            f"{step.get('subject')} —{step.get('predicate_label')}→ {step.get('object')}"
            + (f"（该环节属性：{extra}）" if extra else "")
            + f"（出处：{where}）"
        )
    lines.append("")
    if chain.get("gaps"):
        lines.append(
            "以下环节没有任何出处，必须按规则说明：" + "；".join(str(g) for g in chain["gaps"])
        )
    else:
        lines.append("以上环节全部有出处，不要再补「未提供依据」之类的说明。")
    return "\n".join(lines)


def allowed_numbers(chain: dict[str, Any]) -> set[str]:
    """骨架里出现过的所有数值，作为校验白名单。"""
    parts: list[str] = [str(chain.get("subject") or "")]
    parts.extend(str(c) for c in chain.get("conditions") or [])
    for step in chain.get("steps") or []:
        parts.extend(
            str(step.get(key) or "")
            for key in ("subject", "predicate_label", "object", "hop")
        )
        for item in step.get("evidence") or []:
            parts.extend(str(item.get(key) or "") for key in ("doc_name", "page", "printed_page", "text"))
        # 节点属性（控泄流量、水位…）也是骨架给的数，正文引用它们不算编造
        parts.extend(str(v) for v in (step.get("object_props") or {}).values())
    return {m.group(0) for text in parts for m in _NUMBER_RE.finditer(text)}


def verify_narrative(text: str, chain: dict[str, Any]) -> None:
    """校验正文没有编造数值；不合格抛 `NarrativeRejected`。"""
    allowed = allowed_numbers(chain)
    for match in _NUMBER_RE.finditer(text):
        number = match.group(0)
        if number in allowed:
            continue
        # 允许"第 1 步"这类序号（骨架里本来就有 1..n 的步号）
        if number.isdigit() and 1 <= int(number) <= len(chain.get("steps") or []):
            continue
        raise NarrativeRejected(f"正文出现了骨架里没有的数值：{number}")


async def _collect(model: Any, messages: list[Any]) -> str:
    """统一用 astream 取全文（fake 模型只有 astream；真模型也支持）。"""
    chunks: list[str] = []
    async for item in model.astream(messages):
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        if content:
            chunks.append(str(content))
    return "".join(chunks).strip()


async def write_narrative(
    question: str,
    chain: dict[str, Any] | None,
    *,
    model_factory: Callable[[float], Any] | None = None,
) -> tuple[str | None, str | None]:
    """生成正文。返回 (正文, 放弃原因)；成功时原因部分为 None。

    调用方拿到 (None, 原因) 时应当保留骨架并如实告知，不要把原因吞掉。
    """
    if not chain or not chain.get("steps"):
        return None, "没有可叙述的链路"

    if model_factory is None:
        from app.services.chat import build_chat_model

        model_factory = build_chat_model

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except ImportError:  # pragma: no cover - 正常情况下 langchain 一定在
        return None, "缺少 langchain 依赖"

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=build_user_prompt(question, chain)),
    ]
    try:
        model = model_factory(TEMPERATURE)
        text = await _collect(model, messages)
    except Exception as exc:  # noqa: BLE001 - 模型侧任何失败都不该影响问答
        logger.warning("链路措辞失败：%s", exc)
        return None, f"措辞失败：{exc}"

    if not text:
        return None, "模型没有返回内容"
    try:
        verify_narrative(text, chain)
    except NarrativeRejected as exc:
        logger.warning("链路措辞被拒：%s", exc)
        return None, str(exc)
    return text, None
