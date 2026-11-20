"""证据回找：把「文件 + 页码 + 逐字引句」换算成具体的切片（chunk_id）。

为什么要有单独一层：**本体数据里只该存人能看懂的出处**——哪份文件、第几页、
原文哪一句。切片 id 是入库产物，换一次解析器、重灌一次文档就全变了；
把 chunk_id 写进数据文件，等于把数据绑死在某一版解析结果上。

所以数据文件只写 `{doc, quote, page}`，由这里在导入时回数据库找切片：

- 找不到 → **报错，不写入**。宁可少建一条，也不要建一条点不回原文的"事实"。
- 页码只用来**缩小候选**，不用来决定内容；文档分页与印刷页码不一致时仍然能落位。
- 多个候选时取**最短**的那片：同一段话常同时存在于父块和子块里，父块会把整节
  都带进来，喂给模型时前面的无关内容会把真正要引的那句挤掉。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document


def norm(text: str) -> str:
    """比对用的归一化：去空白、统一上下标与全角括号。

    MinerU 会把数字拆开（`1 3 0 m^3 / s`），所以必须去空白再比；
    上标 ³ 与 ^3 写法不一，也一并归一。
    """
    out = (text or "").replace("³", "3").replace("^", "").replace("&nbsp;", "")
    out = out.replace("（", "(").replace("）", ")").replace("，", ",").replace("：", ":")
    out = re.sub(r"[\s　]+", "", out)
    return out


#: 切片索引：文档名 → [(chunk_id, 归一化正文, 印刷页码)]
ChunkIndex = dict[str, list[tuple[int, str, int | None]]]


async def load_index(db: AsyncSession) -> ChunkIndex:
    """读出所有文档的**当前版本**切片，按文档名分组。

    只取 active_version：历史版本的切片还在库里，混进来会引到旧内容。
    """
    rows = (
        await db.execute(
            select(Document.id, Document.filename, Chunk.id, Chunk.content, Chunk.printed_page)
            .join(Chunk, Chunk.document_version_id == Document.active_version_id)
        )
    ).all()
    out: ChunkIndex = {}
    for _doc_id, filename, chunk_id, content, printed in rows:
        out.setdefault(filename, []).append((int(chunk_id), norm(content), printed))
    return out


def resolve(
    index: ChunkIndex,
    doc: str,
    quote: str,
    page: int | None = None,
) -> dict[str, Any] | None:
    """按文件 + 引句（+ 印刷页码）找切片；找不到返回 None（调用方记为错误）。

    为什么需要页码：预警四个等级的处置措施**句式几乎一模一样**
    （「应在 1h 内将应急响应命令传送到……」「每 2h 汇报……」），
    只按引句匹配会取到第一处——Ⅲ级的关系被挂上 Ⅳ级的原文，回答里就张冠李戴。
    """
    needle = norm(quote)
    if not needle:
        return None
    candidates = [
        (chunk_id, body, printed)
        for chunk_id, body, printed in index.get(doc, [])
        if needle in body
    ]
    if page is not None:
        candidates = [c for c in candidates if c[2] == page]
    # 取最短的那片：同一段话常同时存在于父块和子块里，父块会把整节都带进来。
    candidates.sort(key=lambda c: len(c[1]))
    if not candidates:
        return None
    chunk_id, _body, printed = candidates[0]
    return {
        "type": "原文",
        "doc_name": doc,
        "chunk_id": chunk_id,
        "printed_page": printed,
        "quote": quote,
        "note": quote,
    }
