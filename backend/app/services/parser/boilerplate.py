"""水印/广告噪声过滤：跨页重复行（页眉页脚水印）+ LLM 兜底判断单页偶发广告。

「只增不减」原则的例外：广告/水印不是知识，是检索噪声，用户明确要求过滤。
- **跨页重复检测（确定性主机制）**：归一化行在 ≥max(3, 30% 页数) 的**不同页**出现，
  视为页眉/页脚/水印 → 移除。每页必现的水印（如「钢管购买热线：xxx」「https://…」）
  100% 命中，零 LLM 成本。限行长 [4,60]——长重复句多为正文，不误删。
- **LLM 兜底**：不跨页重复但含广告特征（URL/电话/版权/版本/宣传语）的行，每文档一次
  `ainvoke` 让 LLM 判定是否广告 → 移除。fake/异常 → 降级为纯规则（不阻塞入库）。
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict

from app.services.parser.base import ParsedBlock

logger = logging.getLogger(__name__)

# 广告特征（候选筛选用，非直接过滤——最终判断交给 LLM）
_AD_FEATURE_RE = re.compile(
    r"https?://|www\.|\.com|\.cn|热线|电话|微信|QQ|版权|出版社|引用于|限内部使用"
    r"|严禁用于商业|购买|订购|咨询热线|内部资料|仅供学习交流|第[一二三四五六七八九十0-9]+版",
    re.IGNORECASE,
)
# 水印行长度范围（归一化后）：太短=页码/标记，太长=长句正文，都不判水印
_MIN_LEN, _MAX_LEN = 4, 60


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def filter_repeated_lines(
    blocks: list[ParsedBlock], page_count: int, min_pages: int | None = None
) -> tuple[list[ParsedBlock], set[str], list[str]]:
    """跨页重复行过滤：出现在 ≥max(min_pages, 30% 页数) 的不同页的行视为页眉/页脚/水印。

    返回 (new_blocks, repeated_set, removed_lines)。块因移除变空则删除该块；
    标题块被滤空时保留原样（避免丢失标题）。
    """
    threshold = max(min_pages or 3, int(page_count * 0.3))
    line_pages: dict[str, set[int]] = defaultdict(set)
    for b in blocks:
        if not b.text or not b.page:
            continue
        for ln in b.text.split("\n"):
            t = _norm(ln)
            if _MIN_LEN <= len(t) <= _MAX_LEN:
                line_pages[t].add(b.page)
    repeated = {t for t, pages in line_pages.items() if len(pages) >= threshold}
    if not repeated:
        return blocks, repeated, []
    removed: list[str] = []
    out: list[ParsedBlock] = []
    for b in blocks:
        if not b.text:
            continue
        kept = []
        for ln in b.text.split("\n"):
            if _norm(ln) in repeated:
                s = ln.strip()
                if s and s not in removed:
                    removed.append(s)
            else:
                kept.append(ln)
        new_text = "\n".join(kept).strip()
        if new_text:
            out.append(
                ParsedBlock(text=new_text, section=b.section, page=b.page, block_type=b.block_type)
            )
        elif b.block_type == "heading":
            out.append(b)  # 标题被滤空：保留原样（避免丢失标题）
    return out, repeated, removed


def filter_text_lines(text: str, repeated: set[str]) -> str:
    """从页文本移除重复（水印）行（用于目录页原文）。"""
    if not text or not repeated:
        return text
    kept = [ln for ln in text.split("\n") if _norm(ln) not in repeated]
    return "\n".join(kept).strip()


def collect_ad_candidates(blocks: list[ParsedBlock]) -> list[str]:
    """不跨页重复但含广告特征的行 → LLM 候选（去重、保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for b in blocks:
        if not b.text:
            continue
        for ln in b.text.split("\n"):
            t = _norm(ln)
            if not t or len(t) < 4 or len(t) > 80:
                continue
            if t in seen:
                continue
            if _AD_FEATURE_RE.search(ln):
                seen.add(t)
                out.append(ln.strip())
    return out


_AD_PROMPT = """你是文档清洗助手。以下是某工程技术规范/指南中提取的行，其中可能混有**广告 / 水印 / 版权声明**噪声
（如「引用于《…》某出版社某年某月第一版」「本资料限内部使用，严禁用于商业」「钢管购买热线：xxx」「https://…」
「内部资料」等），这些不是技术知识内容，不应进入知识库。
请只标出**确定是广告/水印/版权声明噪声**的行号（JSON 数组），例如 [0, 3]。技术条款、公式、正文一律不要标。

行列表：
{lines}"""


async def llm_filter_ads(candidates: list[str]) -> list[str]:
    """LLM 判定候选行中的广告 → 返回要移除的行。fake/异常 → []（降级纯规则，不阻塞入库）。"""
    if not candidates:
        return []
    try:
        from app.services.chat import build_chat_model

        llm = build_chat_model(0.0)
        prompt = _AD_PROMPT.format(
            lines="\n".join(f"[{i}] {ln}" for i, ln in enumerate(candidates))
        )
        resp = await llm.ainvoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        idxs = json.loads(text.strip().strip("`"))
        return [candidates[i] for i in idxs if isinstance(i, int) and 0 <= i < len(candidates)]
    except Exception:
        logger.exception("LLM 广告判断失败，降级为纯规则过滤")
        return []


def remove_lines(blocks: list[ParsedBlock], remove: set[str]) -> list[ParsedBlock]:
    """从块中移除指定行（LLM 判定的广告）。块变空删块，标题保留。"""
    if not remove:
        return blocks
    remove_norm = {_norm(x) for x in remove}
    out: list[ParsedBlock] = []
    for b in blocks:
        if not b.text:
            continue
        kept = [ln for ln in b.text.split("\n") if _norm(ln) not in remove_norm]
        new_text = "\n".join(kept).strip()
        if new_text:
            out.append(
                ParsedBlock(text=new_text, section=b.section, page=b.page, block_type=b.block_type)
            )
        elif b.block_type == "heading":
            out.append(b)
    return out
