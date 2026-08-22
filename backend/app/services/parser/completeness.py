"""切片内容完整性自检：原文件每个块的每行必须出现在至少一个切片中。

「只增不减」的自动守卫——大纲只负责标注/补全章节，绝不省略原文件内容。
若某行丢失，说明解析/切分有遗漏，记入 quality["content_completeness"] 供答辩与排查。

判定规则（避免误报）：
- 逐行归一化（去空白）后检查是否包含于任一切片内容；
- 短行（归一化 < 4 字）豁免：可能为页码/标记等被页眉页码过滤，属正常；
- 超长行（归一化 > 100 字）豁免：RecursiveCharacterTextSplitter 会从中切断，
  整行不再连续出现在单个切片（字符仍在，只是跨块）。
"""
from __future__ import annotations

import re


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def check_content_completeness(blocks, chunks) -> dict:
    """对每个块逐行检查原文字是否全部保留在切片中，并核对**页级归属**。

    blocks: list[ParsedBlock]；chunks: list[Chunk]。
    返回 {"complete", "missing_lines", "missing_pages", "skipped_long_lines", "sample",
          "page_coverage"}。page_coverage：有正文内容（非 heading）的页必须存在 page
    相同的 chunk——页号错位（内容被合并进相邻页 chunk）即报 uncovered。
    """
    blob = " ".join(_norm(c.content) for c in chunks)
    missing: list[dict] = []
    skipped_long = 0
    for b in blocks:
        text = (b.text or "").strip()
        if not text:
            continue
        for line in text.split("\n"):
            t = _norm(line)
            if not t or len(t) < 4:
                continue
            if len(t) > 100:
                skipped_long += 1
                continue
            if t not in blob:
                missing.append(
                    {
                        "page": b.page,
                        "type": b.block_type,
                        "text": line.strip()[:80],
                    }
                )
    # 页级覆盖：有正文内容的页必须有 page 相同的 chunk（纯标题页豁免——标题只作前缀）
    content_pages = sorted({b.page for b in blocks if b.page and b.block_type != "heading"})
    chunk_pages = {c.page for c in chunks if c.page}
    uncovered_pages = [p for p in content_pages if p not in chunk_pages]
    return {
        "complete": not missing,
        "missing_lines": len(missing),
        "missing_pages": sorted({m["page"] for m in missing if m["page"]}),
        "skipped_long_lines": skipped_long,
        "sample": missing[:5],
        "page_coverage": {
            "complete": not uncovered_pages,
            "content_pages": len(content_pages),
            "chunk_pages": len(chunk_pages),
            "uncovered_pages": uncovered_pages,
        },
    }
