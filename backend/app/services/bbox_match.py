"""把切片对齐到 MinerU 解析出的文本块，拿到它在原 PDF 上的坐标。

只解决一件事：**扫描件没有文本层**，PyMuPDF 搜不到字，光有页码指不出位置。
MinerU 解析时本来就算出了每个块的 bbox（`middle.json`，PDF 点坐标），只是当初没存。

对齐靠内容而不是顺序：切片文本 vs 块文本，忽略空白与 markdown 记号后比字符 6-gram
覆盖率。表格块在 middle.json 里**没有正文**（正文在 content_list 的 html 里），
只有表题，所以表格单独按表题匹配；页上只有一张表时直接认它。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: 归一化时抹掉：空白、竖线、markdown 记号（切片里的 `## 标题`、`**强调**` 原文没有）
_IGNORED = re.compile(r"[\s|｜#*>]+")
#: 6-gram 覆盖面阈值。低于它就当没对上，宁可不给框。
MIN_SCORE = 0.5
#: 正文块至少要这么长才当候选。标题块短，随便几个字重叠就能凑够阈值，
#: 实测把正文切片错配到"2.3.3 水面蒸发量估计"这种小标题上。
MIN_TEXT_LEN = 24
#: 切片本身很短（就是一句标题）时，才允许落在标题块上
SHORT_CHUNK = 30


def normalize(text: str) -> str:
    return _IGNORED.sub("", text or "")


def shingles(text: str, k: int = 6) -> set[str]:
    if not text:
        return set()
    if len(text) <= k:
        return {text}
    return {text[i : i + k] for i in range(len(text) - k + 1)}


@dataclass(frozen=True)
class BlockRect:
    """一个候选块：类型、坐标、可比较的文本（表格是表题）。"""

    kind: str
    rect: tuple[float, float, float, float]
    text: str


def iter_lines(block: dict):
    """遍历块里的所有行。

    middle.json 有两种形态：普通文本块的 `lines` **直接挂在块上**；
    表格块则是 `blocks: [{type: table_caption|table_body, lines: [...]}]`。
    只认其中一种，取出来的文本就是空串——第一版踩过，命中率 6%。
    """
    if block.get("lines"):
        yield block
        return
    for sub in block.get("blocks") or []:
        if sub.get("lines"):
            yield sub


def block_text(kind: str, spans: str, caption: str) -> str:
    """表格块只留表题；正文块用 OCR 出来的文字。"""
    if kind == "table":
        return normalize(caption)
    return normalize(spans)


def score_block(block: BlockRect, chunk_text: str, *, chunk_is_table: bool) -> float:
    """切片落在这个块上的可信度（0~1）。"""
    chunk_norm = normalize(chunk_text)
    if not chunk_norm:
        return 0.0

    if block.kind == "table":
        # 表题能直接对上最可靠（切片里通常带着「表5.2 …」）
        if block.text and block.text in chunk_norm:
            return 1.0
        # 表题对不上时，只有"切片本身是表格"才勉强认（调用方还会判页面是否只有一张表）
        return 0.2 if chunk_is_table else 0.0

    block_sets = shingles(block.text)
    if not block_sets:
        return 0.0
    if block.text in chunk_norm:
        return 1.0
    chunk_sets = shingles(chunk_norm)
    return len(block_sets & chunk_sets) / len(block_sets)


def pick_block(
    blocks: list[BlockRect],
    chunk_text: str,
    *,
    chunk_is_table: bool,
    min_score: float = MIN_SCORE,
) -> BlockRect | None:
    """挑出切片对应的块；都不够格就返回 None（界面上宁可"定位不到"也不乱框）。"""
    if not blocks:
        return None

    tables = [b for b in blocks if b.kind == "table"]
    # 页上只有一张表、切片又是表格块 → 就是它（扫描件的表格切片常没有表题）
    if chunk_is_table and len(tables) == 1:
        return tables[0]

    chunk_norm = normalize(chunk_text)
    short_chunk = len(chunk_norm) <= SHORT_CHUNK

    best: BlockRect | None = None
    best_score = 0.0
    for block in blocks:
        # 切片是表格时，不该落到普通段落上（反之亦然）
        if chunk_is_table != (block.kind == "table"):
            continue
        # 短标题不配当长正文的落点
        if block.kind in {"title"} and not short_chunk:
            continue
        if block.kind != "table" and len(block.text) < MIN_TEXT_LEN and not short_chunk:
            continue
        value = score_block(block, chunk_text, chunk_is_table=chunk_is_table)
        # 分数接近时优先长块：正文段落比小标题更能代表"这一段在哪"
        if value > best_score + 0.05 or (
            abs(value - best_score) <= 0.05 and best is not None and len(block.text) > len(best.text)
        ):
            best_score = value
            best = block
    if best is not None and best_score >= min_score:
        return best

    # 类型判断不靠谱时（解析器没标 block_type）再放宽一次
    for block in blocks:
        value = score_block(block, chunk_text, chunk_is_table=chunk_is_table)
        if value > best_score:
            best_score = value
            best = block
    return best if best_score >= min_score else None


def unions(rects: list[tuple[float, float, float, float]]) -> list[list[float]]:
    """把标题和表体这类相邻块并成一个框，避免两个框叠着画。"""
    xs0 = [r[0] for r in rects]
    ys0 = [r[1] for r in rects]
    xs1 = [r[2] for r in rects]
    ys1 = [r[3] for r in rects]
    return [[min(xs0), min(ys0), max(xs1), max(ys1)]]
