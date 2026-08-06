"""条款断号自检：检测同节内连续编号缺失（OCR 偶发漏行的信号）。

背景：RapidOCR 整页识别在密集排版下偶发漏掉个别条款行（实测漏读 8.2.3），
导致最终切片/答案缺条款。入库后对解析块做断号扫描，命中则触发更高条带数重 OCR 修复。

注意：检测可能误报（规范本身跳号）。修复环节用「重 OCR 是否补回缺失条款号」自校验，
补不回（规范本来就无该条）则不改动，天然免疫误报。
"""
from __future__ import annotations

import re

# 条款号：N.N.N（如 8.2.3），前后不为数字/点（排除长串版本号、日期尾段）
_CLAUSE_RE = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2}\.\d{1,2})(?![\d])")

# 断号自检是否启用
def _is_clause(num_str: str) -> bool:
    """过滤明显非条款号：首段 >=100（如日期 2020.10.01）或末段 >99。"""
    parts = num_str.split(".")
    try:
        return int(parts[0]) < 100 and int(parts[2]) <= 99
    except ValueError:
        return False


def _contains_clause(text: str, full_num: str) -> bool:
    """文本中是否出现该条款号（防部分匹配：8.2.3 不匹配 8.2.30）。"""
    return re.search(rf"(?<![\d.]){re.escape(full_num)}(?![\d])", text) is not None


def check_clause_gaps(blocks) -> list[dict]:
    """扫描解析块，返回疑似断号的节。

    blocks: 可迭代对象，元素需有 `.text`（块文本）、`.section`、`.page`。
    返回: [{section, present:[..], missing:[..], missing_full:[..], pages:[..]}]
      - present/missing 为本节 N.N 下的条款序号（int）
      - missing_full 为完整条款号（如 "8.2.3"），供修复校验
      - pages 为命中该节的页号（去重升序）
    """
    from collections import defaultdict

    per_sec: dict[str, list[tuple[int, int | None]]] = defaultdict(list)  # sec -> [(num, page)]
    for b in blocks:
        if not b.text:
            continue
        for m in _CLAUSE_RE.findall(b.text):
            if not _is_clause(m):
                continue
            sec = ".".join(m.split(".")[:2])
            try:
                num = int(m.split(".")[2])
            except ValueError:
                continue
            per_sec[sec].append((num, b.page))

    gaps: list[dict] = []
    for sec, items in sorted(per_sec.items(), key=lambda kv: [int(x) for x in kv[0].split(".")]):
        nums = sorted({n for n, _ in items})
        if len(nums) < 2:
            continue  # 单条款无法判断缺失
        missing = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
        if not missing:
            continue
        pages = sorted({p for _, p in items if p})
        gaps.append(
            {
                "section": sec,
                "present": nums,
                "missing": missing,
                "missing_full": [f"{sec}.{n}" for n in missing],
                "pages": pages,
            }
        )
    return gaps
