"""问题中点名文档 → 检索限定（G6-2 从 rag.py 抽出）。

支持《书名号》与「XXX中」两种点名方式；匹配要求长短比 ≥0.4，防「预案」这类
泛词误中整份文档名。kb_id 非空时只在该库内匹配（P0-2 scope 隔离）。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 《书名号》引用；「XXX中」引用（XXX 在「中」前，如「重庆市防汛抗旱应急预案中后期处置…」）
_DOC_TITLE_RE = re.compile(r"《([^《》]{2,80})》")
# 文档泛指词：中分句里若是这类泛词（规范/标准/预案…）不当作点名文档
_DOC_GENERIC = {"规范", "标准", "预案", "文件", "文档", "规定", "指南", "导则", "资料", "制度", "办法", "规程", "要求", "内容"}
_DOC_EXT = (".pdf", ".docx", ".md", ".markdown", ".txt", ".xlsx", ".csv")


def _strip_ext(name: str) -> str:
    for ext in _DOC_EXT:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def doc_name_candidates(query: str) -> list[str]:
    """提取问题中点名的文档名候选：《…》内容 + 「XXX中」的 XXX（去在/关于前缀）。"""
    cands: list[str] = []
    for t in _DOC_TITLE_RE.findall(query):
        cands.append(t.strip())
    for seg in query.split("中"):
        s = re.sub(r"^(在|关于|对于|就)\s*", "", seg.strip())
        s = s.replace("《", "").replace("》", "")
        if 2 <= len(s) <= 40 and s not in _DOC_GENERIC:
            cands.append(s)
    return cands


def match_document(docs, cand: str):
    """候选名匹配文档：精确优先；子串匹配要求「长短比 ≥ 0.4」防泛词误中（如「预案」命中整份预案名）。"""
    exact = [d for d in docs if _strip_ext(d.filename) == cand]
    if exact:
        return exact[0]
    best, best_ratio = None, 0.0
    for d in docs:
        fn = _strip_ext(d.filename)
        if cand in fn or fn in cand:
            short, long = min(len(cand), len(fn)), max(len(cand), len(fn))
            ratio = short / long if long else 0.0
            if ratio >= 0.4 and ratio > best_ratio:
                best, best_ratio = d, ratio
    return best


async def resolve_documents_by_title(
    db: AsyncSession, query: str, kb_id: int | None = None
) -> list[int]:
    """从问题中提取书名/文档名（《…》或「XXX中」），匹配知识库文档，返回 doc_id 列表。

    kb_id 非空时只匹配该库文档（P0-2 scope 隔离：KB-A 里点名 KB-B 的同名文档 → 返回空，
    检索回退当前库，杜绝跨库污染）。kb_id=None（跨全部库模式）匹配全库。
    匹配不到（宽泛问法如「在数字孪生工程中…」、泛词如「标准中规定…」）返回空列表，
    检索回退跨库。支持多书名（对比两份规范 → 返回多个 doc_id）。
    """
    from app.db.models import Document

    stmt = select(Document)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    docs = (await db.scalars(stmt)).all()
    if not docs:
        return []
    result: list[int] = []
    for cand in doc_name_candidates(query):
        best = match_document(docs, cand)
        if best and best.id not in result:
            result.append(best.id)
    return result
