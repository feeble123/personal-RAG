"""LLM 断号补全：扫描正文编号 → 算法候选 → LLM 确认（裁判）→ 供注入软边界。

LLM 只当「防误报裁判」：只能从算法候选集里筛，不能新增编号，防幻觉造号。
离线（fake/超时/异常）时降级为「全部候选确认」，行为确定、不阻塞入库。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from app.core.config import settings
from app.services.chat import build_chat_model
from app.services.parser.toc import TocInfo
from app.services.verify import _parse_json

logger = logging.getLogger(__name__)


@dataclass
class FoundNumber:
    """正文中一个「行首编号」出现点（含定位，供注入拆段落）。"""

    number: str
    text: str
    page: int | None
    block_index: int
    line_index: int


# 行首编号（放宽，不要求标题形状）：1 / 1.2 / 2.1.3（最多 4 段）
_NUM_START_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,3})\.?\s*")


def scan_numbered_lines(blocks) -> list[FoundNumber]:
    """扫描全部正文块的「行首编号」，返回定位列表。"""
    found: list[FoundNumber] = []
    for bi, b in enumerate(blocks):
        if not b.text:
            continue
        for li, line in enumerate(b.text.split("\n")):
            m = _NUM_START_RE.match(line.strip())
            if not m:
                continue
            number = m.group(1)
            if int(number.split(".")[0]) >= 100:  # 排除日期 2020.10.01 之类
                continue
            found.append(FoundNumber(number=number, text=line.strip(), page=b.page, block_index=bi, line_index=li))
    return found


def _num_key(num: str) -> list[int]:
    return [int(x) for x in num.split(".") if x]


def candidate_missing(toc_info: TocInfo | None, found: list[FoundNumber]) -> list[str]:
    """算法候选（纯函数）：目录缺失 + 同级兄弟连续段缺口（上下邻居都在）。

    b 类「上下邻居都在 found 中」防止凭空猜号：1.1、1.3 在而 1.2 缺 → 1.2 候选。
    """
    cands: set[str] = set()
    found_nums = {f.number for f in found}

    # a) 目录为准：TOC 有、正文没识别到（level<=4，防目录深层噪声）
    if toc_info:
        for e in toc_info.entries:
            if e.number and e.level <= 4 and e.number not in found_nums:
                cands.add(e.number)

    # b) 同级兄弟连续段缺口：按 (level, parent) 分组，缺失值的上下邻居都在
    by_parent: dict[tuple[int, str], set[int]] = defaultdict(set)
    for f in found:
        parts = f.number.split(".")
        if len(parts) < 2:
            continue
        parent = ".".join(parts[:-1])
        by_parent[(len(parts), parent)].add(int(parts[-1]))
    for (level, parent), nums in by_parent.items():
        if len(nums) < 2:
            continue
        for m in range(min(nums) + 1, max(nums)):
            if m not in nums and (m - 1) in nums and (m + 1) in nums:
                cands.add(f"{parent}.{m}")

    return sorted(cands, key=_num_key)


def _format_toc(toc_info: TocInfo | None) -> str:
    if not toc_info:
        return "（无目录）"
    lines = [f"{e.number} {e.title}" for e in toc_info.entries[:200] if e.number]
    return "\n".join(lines) or "（无目录）"


def _format_found(found: list[FoundNumber]) -> str:
    """按 1/2 级父节分组压缩，控制提示词长度。"""
    groups: dict[str, list[str]] = defaultdict(list)
    for f in found:
        parts = f.number.split(".")
        parent = ".".join(parts[:2]) if len(parts) >= 2 else parts[0]
        groups[parent].append(f.number)
    out = []
    for parent in sorted(groups, key=_num_key):
        out.append(f"{parent}: {','.join(sorted(groups[parent], key=_num_key)[:80])}")
    return "\n".join(out)[:6000]


_GAP_PROMPT = """判断以下章节编号中哪些在文档里**可能缺失**，只输出 JSON。

【目录大纲】（若有，最可靠依据）
{toc}

【正文已识别编号】（按节分组）
{found}

【待确认候选】（只能从这里选）
{candidates}

规则：
1. 同级兄弟编号通常连续：若 1.1 和 1.3 都存在而 1.2 缺失，且目录/上下文支持 1.2 存在，则 1.2 可能缺失。
2. 目录中有但正文没识别的编号，缺失可能性高。
3. 区分真实缺失 vs 合法跳号（版本修订删节）：以目录为最可靠依据；目录没有的，仅当上下邻居都在时才判。
4. **只能从【待确认候选】里选**，不得新增候选外的编号；不确定就少报。

输出严格 JSON：{{"missing": ["1.2", "2.1.3"]}}（无则 []）"""


async def confirm_missing(toc_info: TocInfo | None, found: list[FoundNumber]) -> set[str] | None:
    """LLM 确认缺失编号。每文档一次 ainvoke；异常/离线返回 None（调用方降级为全候选）。"""
    candidates = candidate_missing(toc_info, found)
    if not candidates:
        return set()
    prompt = _GAP_PROMPT.format(
        toc=_format_toc(toc_info),
        found=_format_found(found),
        candidates="、".join(candidates),
    )
    try:
        llm = build_chat_model(0.0)
        resp = await llm.ainvoke(
            [("system", "你是严谨的结构质检员，只输出 JSON，不要输出其他内容。"), ("human", prompt)]
        )
        text = getattr(resp, "content", "") or ""
        data = _parse_json(text)
        confirmed = data.get("missing") or []
        if not isinstance(confirmed, list):
            confirmed = []
        confirmed = {str(c).strip() for c in confirmed}
        # LLM 只能从候选里筛（防幻觉造号）
        confirmed &= set(candidates)
        logger.info("LLM 断号确认：候选 %d 个，确认 %d 个", len(candidates), len(confirmed))
        return confirmed
    except Exception:
        logger.warning("LLM 断号确认失败，降级为全部候选（不阻塞入库）", exc_info=True)
        return None
