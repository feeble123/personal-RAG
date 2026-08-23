"""LLM 答案校验服务。

层2（完备性）：枚举/概述类问题生成后，判断回答是否完整覆盖了检索证据指向的章节条目；
不完整 → 调用方扩大证据重新生成。产出「完备率」指标。
层3（引用忠实）：校验回答每个 [n] 是否被对应引用支撑，产出「引用准确率」指标。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.core.config import settings
from app.services.chat import build_chat_model

logger = logging.getLogger(__name__)


@dataclass
class CompletenessVerdict:
    enumeration: bool = False  # 该问题是否要求完整枚举/概述
    complete: bool = True      # 回答是否完整覆盖（仅 enumeration=True 时有效）
    note: str = ""             # 一句话说明 / 遗漏指向


_COMPLETENESS_PROMPT = """判断下面的回答是否完整枚举了问题要求的内容，只输出 JSON。

【问题】{query}
【回答】{answer}
【检索到的资料清单】（仅文件名/章节/页码范围，用于判断回答是否用全了资料）
{cites}

判定规则：
1. enumeration：该问题是否要求「完整列出/枚举/概述全部」某类内容（成员/方案/单位/条目/要求等）？
2. complete：若 enumeration 为 true，回答是否完整覆盖了资料清单中相关章节能提供的全部条目？
   若回答出现「只有X个」「未列出全部」「资料未覆盖」等话术，而资料清单里对应章节明显可能包含更多条目，
   则 complete 为 false。
3. note：一句话说明；若 complete=false，指出遗漏指向的章节/内容。

输出严格 JSON：{{"enumeration": true/false, "complete": true/false, "note": "..."}}"""


def _compact_cites(cites) -> str:
    """把检索证据压缩成「文件名/章节/页码」清单（不塞全文，控制校验成本）。"""
    lines = []
    for c in cites:
        loc = c.source
        if getattr(c, "page", None):
            loc += f" 第{c.page}页"
        if getattr(c, "section", None):
            loc += f" [{c.section}]"
        lines.append(f"- {loc}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（兼容前后杂散文字）。"""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _head_tail(text: str, head: int = 2400, tail: int = 1600) -> str:
    """长回答压缩为「开头 + 结尾」：枚举/表格被截断时坏在**尾部**，
    只给前 3000 字会让校验器漏判。首尾都展示 → 能看到列表确实写完 / 中途截断。"""
    text = text or ""
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n……[中间内容省略，仅展示首尾以判断是否截断]……\n" + text[-tail:]


async def verify_completeness(query: str, answer: str, cites) -> CompletenessVerdict:
    """完备性校验：枚举类问题是否答全。失败时返回「不枚举/完整」默认值（不阻塞主流程）。"""
    if not answer or not cites:
        return CompletenessVerdict()
    prompt = _COMPLETENESS_PROMPT.format(
        query=query[:500],
        answer=_head_tail(answer, head=2400, tail=1600),
        cites=_compact_cites(cites)[:3000],
    )
    llm = build_chat_model(0.0)
    messages: list[tuple[str, str]] = [
        ("system", "你是严谨的质检员，只输出 JSON，不要输出其他内容。"),
        ("human", prompt),
    ]
    try:
        resp = await llm.ainvoke(messages)
        text = getattr(resp, "content", "") or ""
        data = _parse_json(text)
        return CompletenessVerdict(
            enumeration=bool(data.get("enumeration")),
            complete=bool(data.get("complete", True)),
            note=str(data.get("note", ""))[:300],
        )
    except Exception:
        logger.warning("完备性校验失败，跳过（不阻塞问答）", exc_info=True)
        return CompletenessVerdict()


# ---- 层3：引用忠实校验（单元3 使用）----
@dataclass
class CitationVerdict:
    ok: bool = True
    bad_numbers: list[int] = None  # 无效/不被支撑的引用编号
    note: str = ""


_CITATION_PROMPT = """核对回答中的引用编号 [n] 是否被对应资料支撑，只输出 JSON。

【回答】{answer}
【资料】
{cites}

规则：回答里标注的 [n] 必须对应下方第 n 条资料，且该条资料的内容确实支撑了回答中标注处的主张。
若某编号超出资料条数、或资料内容与主张无关/编造 → 该引用无效。

输出严格 JSON：{{"ok": true/false, "bad_numbers": [无效的编号], "note": "..."}}"""


def _numbered_cites(cites) -> str:
    lines = []
    for i, c in enumerate(cites, start=1):
        loc = c.source
        if getattr(c, "page", None):
            loc += f" 第{c.page}页"
        snippet = getattr(c, "snippet", "") or ""
        lines.append(f"[{i}] {loc}: {snippet[:200]}")
    return "\n".join(lines)


def _out_of_range_citations(answer: str, cites) -> list[int]:
    """确定性超范围引用检测（不依赖 LLM）：回答里 [n] 且 n > 资料条数 → 幻觉编号。"""
    import re

    n_cites = len(cites)
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", answer or "")]
    return sorted({n for n in nums if n > n_cites or n < 1})


async def verify_citations(answer: str, cites) -> CitationVerdict:
    """引用忠实校验：回答里的每个 [n] 是否被第 n 条资料真实支撑。

    两层校验：
    1. 确定性：解析 [n]，n 超出资料条数（或 <1）→ 幻觉编号，必判无效
       （防「cites=8 却引用 [15]」这类编造编号）
    2. LLM：对范围内编号判「语义是否被第 n 条支撑」（可选，防 LLM 波动误报）
    """
    if not answer or not cites:
        return CitationVerdict()
    # 第一层：确定性超范围检测（稳定，不随 LLM 波动）
    over = _out_of_range_citations(answer, cites)
    if over:
        return CitationVerdict(ok=False, bad_numbers=over, note=f"引用编号超出资料条数: {over}")

    prompt = _CITATION_PROMPT.format(
        answer=_head_tail(answer, head=3000, tail=1500),
        cites=_numbered_cites(cites)[:4000],
    )
    llm = build_chat_model(0.0)
    messages: list[tuple[str, str]] = [
        ("system", "你是严谨的质检员，只输出 JSON，不要输出其他内容。"),
        ("human", prompt),
    ]
    try:
        resp = await llm.ainvoke(messages)
        text = getattr(resp, "content", "") or ""
        data = _parse_json(text)
        bad = data.get("bad_numbers") or []
        if isinstance(bad, list):
            bad = [int(x) for x in bad if str(x).isdigit()]
        # 第二层：LLM 判「语义不支撑」，但超范围已由第一层排除
        return CitationVerdict(
            ok=bool(data.get("ok", not bad)),
            bad_numbers=bad,
            note=str(data.get("note", ""))[:300],
        )
    except Exception:
        logger.warning("引用校验失败，跳过", exc_info=True)
        return CitationVerdict()
