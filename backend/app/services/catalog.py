"""知识库文件清单（G7）。

用户在问「你拥有哪些知识文件」时，答案来自文档表本身，而不是向量检索。
- 指定了知识库 → 列出该库全部文档（名称/类型/页数/切片数/状态）
- 跨库模式 → 列出所有知识库及各自文档数（避免一次刷出几百个文件名）
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, KnowledgeBase

_DOC_TYPE_ZH = {
    "textbook": "教材",
    "standard": "规范",
    "manual": "手册",
    "other": "其他",
}
_STATUS_ZH = {
    "pending": "排队中",
    "parsing": "解析中",
    "embedding": "向量化中",
    "ready": "已就绪",
    "failed": "解析失败",
}


async def load_catalog(db: AsyncSession, kb_id: int | None = None) -> dict:
    """查知识库/文档清单。返回结构化 dict（供格式化与测试断言）。"""
    if kb_id is not None:
        kb = await db.get(KnowledgeBase, kb_id)
        rows = (
            await db.execute(
                select(Document).where(Document.kb_id == kb_id).order_by(Document.id)
            )
        ).scalars().all()
        items = [
            {
                "filename": d.filename,
                "file_type": d.file_type,
                "doc_type": _DOC_TYPE_ZH.get(d.doc_type, d.doc_type or "其他"),
                "page_count": d.page_count,
                "chunk_count": d.chunk_count,
                "status": d.status,
                "status_zh": _STATUS_ZH.get(d.status, d.status),
            }
            for d in rows
        ]
        counts = {
            "total": len(items),
            "ready": sum(1 for i in items if i["status"] == "ready"),
            "parsing": sum(1 for i in items if i["status"] in ("pending", "parsing", "embedding")),
            "failed": sum(1 for i in items if i["status"] == "failed"),
        }
        return {
            "mode": "kb",
            "kb_id": kb_id,
            "kb_name": kb.name if kb else f"知识库 #{kb_id}",
            "items": items,
            "counts": counts,
        }

    rows = (
        await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))
    ).scalars().all()
    items = [
        {
            "name": k.name,
            "doc_count": k.doc_count,
            "chunk_count": k.chunk_count,
        }
        for k in rows
    ]
    return {"mode": "all", "items": items, "counts": {"total_kbs": len(items)}}


def format_catalog(catalog: dict) -> str:
    """把结构化清单转成给 LLM 的纯文本（不暴露内部 id/字段名）。"""
    if catalog["mode"] == "kb":
        lines = [f"当前知识库：{catalog['kb_name']}"]
        c = catalog["counts"]
        extra = []
        if c["parsing"]:
            extra.append(f"解析中 {c['parsing']}")
        if c["failed"]:
            extra.append(f"失败 {c['failed']}")
        suffix = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"共收录 {c['total']} 份文档{suffix}：")
        if not catalog["items"]:
            lines.append("（该知识库暂无文档）")
        for idx, it in enumerate(catalog["items"], 1):
            pages = f"｜{it['page_count']} 页" if it["page_count"] else ""
            lines.append(
                f"{idx}. {it['filename']}｜{it['doc_type']}｜{it['file_type'].upper()}"
                f"{pages}｜{it['chunk_count']} 个切片｜{it['status_zh']}"
            )
        return "\n".join(lines)

    lines = [f"当前为跨库模式，共 {catalog['counts']['total_kbs']} 个知识库："]
    if not catalog["items"]:
        lines.append("（暂无知识库）")
    for idx, it in enumerate(catalog["items"], 1):
        lines.append(f"{idx}. {it['name']}｜{it['doc_count']} 份文档｜{it['chunk_count']} 个切片")
    lines.append("如需查看某个知识库的具体文件，请先选择该知识库再提问。")
    return "\n".join(lines)
