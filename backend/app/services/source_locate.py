"""在原 PDF 里定位一段引用，返回高亮矩形（PDF 点坐标）。

两条路，谁有走谁：

1. **有文本层**（多数电子版 PDF）：把该页的词框摊平成一条忽略空白/竖线的字符流，
   在流里找引用的归一化串，再映射回词框并按行合并。这跟前端 `findQuoteRange`
   是同一套口径——否则就会出现"前端说能高亮、后端说找不到"的分裂。
2. **没有文本层**（扫描件，如《水文预报》第 5 版）：只能退到切片自带的块级
   bbox（`chunks.bbox`，由 MinerU 的 `middle.json` 回填）。精度到"块"，不到"行"。

两样都没有就明确返回 `source="none"`——**绝不硬指一个框**，那比不标更糟：
用户会以为那句话在那里。

坐标含义：PyMuPDF 的页面坐标系，单位是点（1/72 英寸），origin 左上、y 向下，
与 `page.rect` 一致。前端按 `viewport 宽 / page_size 宽` 等比缩放即可。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 引用里那些"我们为了可读性加进去"的分隔符，原文里并没有
_SEPARATORS = re.compile(r"[\s|｜]+")
# 归一化时抹掉：空白、竖线、以及切片自带的 markdown 记号（`## 4. 模型参数` 这种
# 标题在原文里并没有井号，不去掉就永远对不上）
_IGNORED = re.compile(r"[\s|｜#*>]+")
#: 整句找不到时，退而求其次的最小 token 长度（"CI" 这种两字符代号也要能命中）
_MIN_TOKEN = 2
#: 句子太长时只取前 N 个字去找，避免整段 OCR 噪声导致完全不命中
_MAX_QUOTE = 120


@dataclass(frozen=True)
class LocateResult:
    """定位结果。`source` 说明这个框有多可信。"""

    page: int
    page_count: int
    page_size: tuple[float, float]
    has_text_layer: bool
    source: str  # text / bbox / none
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "page_count": self.page_count,
            "page_size": {"width": self.page_size[0], "height": self.page_size[1]},
            "has_text_layer": self.has_text_layer,
            "source": self.source,
            "rects": [list(r) for r in self.rects],
            "note": self.note,
        }


def normalize(text: str) -> str:
    """去掉空白与竖线。前后端必须一致，否则同一句话两边判断不同。"""
    return _IGNORED.sub("", text or "")


def _page_char_stream(page) -> tuple[str, list[int], list[tuple]]:
    """把整页词框摊平成 (归一化字符流, 每个字符对应的词序号, 词框列表)。"""
    words = page.get_text("words")  # (x0, y0, x1, y1, text, block_no, line_no, word_no)
    chars: list[str] = []
    owners: list[int] = []
    for index, word in enumerate(words):
        for ch in str(word[4]):
            # 与 `normalize` 用同一把尺子：正文里的 markdown 记号也要一并抹掉，
            # 否则"引用去掉了井号、正文没去掉"会直接对不上
            if ch.isspace() or _IGNORED.match(ch):
                continue
            chars.append(ch)
            owners.append(index)
    return "".join(chars), owners, words


def _merge_rects(rects: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """把词框合并成"行"：竖直方向重叠的就并成一条，避免几十个小方块糊成一片。"""
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (round(r[1], 1), r[0]))
    merged: list[list[float]] = []
    for x0, y0, x1, y1 in ordered:
        if merged:
            last = merged[-1]
            # 与上一行竖直重叠超过一半 → 同一行，横向并起来
            overlap = min(last[3], y1) - max(last[1], y0)
            height = min(last[3] - last[1], y1 - y0)
            if height > 0 and overlap / height > 0.5:
                last[0], last[1] = min(last[0], x0), min(last[1], y0)
                last[2], last[3] = max(last[2], x1), max(last[3], y1)
                continue
        merged.append([x0, y0, x1, y1])
    return [(a, b, c, d) for a, b, c, d in merged]


def text_rects(page, quote: str) -> list[tuple[float, float, float, float]]:
    """在文本层里找引用，返回按行合并后的矩形。找不到返回空列表。"""
    needle = normalize(quote)[:_MAX_QUOTE]
    if not needle:
        return []
    chars, owners, words = _page_char_stream(page)
    if not chars:
        return []

    start = chars.find(needle)
    length = len(needle)
    if start < 0:
        # 整句对不上（OCR 错字、跨页、引用被改写）：退到最长 token
        tokens = sorted(
            (t for t in _SEPARATORS.split(normalize(quote)) if len(t) >= _MIN_TOKEN),
            key=len,
            reverse=True,
        )
        for token in tokens:
            hit = chars.find(token)
            if hit >= 0:
                start, length = hit, len(token)
                break
        else:
            return []

    hit_words = {owners[i] for i in range(start, min(start + length, len(owners)))}
    return _merge_rects([tuple(words[i][:4]) for i in sorted(hit_words)])


def bbox_rects(bbox: dict | None) -> list[tuple[float, float, float, float]]:
    """切片自带块级框（U5 回填）。格式不对就当没有，不猜。"""
    if not isinstance(bbox, dict):
        return []
    rects = bbox.get("rects")
    if not isinstance(rects, list):
        return []
    out = []
    for item in rects:
        if isinstance(item, (list, tuple)) and len(item) == 4:
            try:
                out.append(tuple(float(v) for v in item))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return out


def locate_in_page(
    path: Path,
    page_no: int,
    *,
    quote: str | None = None,
    chunk_bbox: dict | None = None,
) -> LocateResult:
    """定位一页：先文本层，再块级 bbox，都不行就 `none`。

    页码超出范围会被夹到有效区间（历史数据里 `page` 偶尔是 0 或 NULL）。
    """
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        page_count = doc.page_count
        if page_count == 0:
            return LocateResult(0, 0, (0.0, 0.0), False, "none", note="这份 PDF 没有页面")
        page_no = max(1, min(int(page_no or 1), page_count))
        page = doc[page_no - 1]
        size = (float(page.rect.width), float(page.rect.height))
        has_text = bool(page.get_text().strip())

        if has_text and quote:
            rects = text_rects(page, quote)
            if rects:
                return LocateResult(page_no, page_count, size, True, "text", rects)

        rects = bbox_rects(chunk_bbox)
        if rects:
            note = None if has_text else "这页是扫描件（没有文本层），框出的是整块区域"
            return LocateResult(page_no, page_count, size, has_text, "bbox", rects, note)

    note = "这页没有文本层，切片也没有坐标，无法标出具体位置" if not has_text else (
        "没在原文里找到这句话（可能被改写、跨页，或切片来自表格重排）"
    )
    return LocateResult(page_no, page_count, size, has_text, "none", [], note)
