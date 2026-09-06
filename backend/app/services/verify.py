"""LLM 答案校验服务。

层2（完备性）：枚举/概述类问题生成后，判断回答是否完整覆盖了检索证据指向的章节条目；
不完整 → 调用方扩大证据重新生成。产出「完备率」指标。
层3（引用忠实）：校验回答每个 [n] 是否被对应引用支撑，产出「编号合规率」+「引用相关率」两指标。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

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


# ---- 层3：引用忠实校验（确定性两层判定，零 LLM）----
@dataclass
class CitationVerdict:
    ok: bool = True                        # 编号合规：所有 [n] 都在资料条数范围内
    bad_numbers: list[int] = None          # 越界/编造的引用编号（编号合规层）
    unsupported_numbers: list[int] = None  # 编号合法但词面不支撑的引用（相关性层）
    note: str = ""


def _out_of_range_citations(answer: str, cites) -> list[int]:
    """确定性超范围引用检测（不依赖 LLM）：回答里 [n] 且 n > 资料条数 → 幻觉编号。"""
    n_cites = len(cites)
    nums = [int(x) for x in re.findall(r"\[(\d+)\]", answer or "")]
    return sorted({n for n in nums if n > n_cites or n < 1})


def _jieba_keywords(text: str) -> set[str]:
    """jieba 分词取实词集合（去掉空白/单字符/纯标点）。用于词面重叠判定。"""
    import jieba

    words = set()
    for w in jieba.lcut(text or ""):
        w = w.strip()
        if len(w) < 2 or w.isspace():
            continue
        if re.fullmatch(r"[\W_]+", w):
            continue
        words.add(w.lower())
    return words


# 标注句与资料原文的 Jaccard 词重叠低于此值 → 判定「不支撑」（挂错引用）
_CITATION_MIN_JACCARD = 0.05


def _sentence_before_citation(answer: str, n: int) -> str:
    """提取回答中第 1 个 [n] 标注前紧邻的一句话（到上一个句号/换行/分号为止）。

    用于判断「给无关句挂 [n]」——取标注所在句的上下文，与第 n 条资料比对。
    """
    matches = list(re.finditer(r"\[(\d+)\]", answer or ""))
    # 取编号等于 n 的标记（通常首个），其前一句为「被标注句」
    nth = [m for m in matches if int(m.group(1)) == n]
    if not nth:
        return ""
    idx = nth[0].start()
    seg = answer[:idx]
    cut = max(seg.rfind("。"), seg.rfind("；"), seg.rfind("\n"), seg.rfind("．"))
    start = cut + 1 if cut >= 0 else max(0, idx - 80)
    return answer[start:idx].strip()


def _word_overlap_supported(answer: str, cites) -> list[int]:
    """确定性「标注句 vs 资料原文」词面重叠检测：重叠过低 → 判定挂错引用。

    零 LLM、可复现。只抓「给无关句挂 [n]」这类明确错挂（中文同义改写不会误报，
    因为同义改写通常仍共享若干实词）。返回不被支撑的编号列表。
    """
    unsupported = []
    n_cites = len(cites)
    for n in range(1, n_cites + 1):
        cite = cites[n - 1]
        snippet = getattr(cite, "snippet", "") or ""
        sent = _sentence_before_citation(answer, n)
        if not sent or not snippet:
            continue
        sent_kw = _jieba_keywords(sent)
        cite_kw = _jieba_keywords(snippet)
        if not sent_kw or not cite_kw:
            continue
        inter = sent_kw & cite_kw
        union = sent_kw | cite_kw
        jaccard = len(inter) / len(union)
        if jaccard < _CITATION_MIN_JACCARD:
            unsupported.append(n)
    return unsupported


async def verify_citations(answer: str, cites) -> CitationVerdict:
    """引用校验：回答里的每个 [n] 是否被第 n 条资料真实支撑。

    以**确定性为主**（稳定、可复现，不随 LLM 波动），分两层：
    1. **编号合规**（ok / bad_numbers）：n 超出资料条数（或 <1）→ 幻觉/编造编号。
    2. **词面支撑**（unsupported_numbers）：n 合法但「标注句 vs 第 n 条资料原文」的
       jieba 词面重叠过低 → 给无关句挂引用（挂错引用）。

    注意：两层相互独立。ok 只代表「编号没越界」，不代表「内容相关」——
    这就是为什么指标名要如实区分「编号合规率」和「引用相关率」，不冒充语义级校验。
    unsupported_numbers 是词面启发式（零 LLM），只抓明确错挂，不做强否定。
    """
    if not answer or not cites:
        return CitationVerdict()
    over = _out_of_range_citations(answer, cites)
    if over:
        return CitationVerdict(
            ok=False, bad_numbers=over, unsupported_numbers=[], note=f"引用编号超出资料条数: {over}"
        )
    unsupported = _word_overlap_supported(answer, cites)
    return CitationVerdict(
        ok=True,
        bad_numbers=[],
        unsupported_numbers=unsupported,
        note=f"词面不支撑的引用: {unsupported}" if unsupported else "",
    )
