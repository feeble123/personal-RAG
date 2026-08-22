"""对外检索服务（P0-11）：稳定契约的唯一入口。

未来 DSH（DeepSeek Harness）通过 HTTP 调用 RAG 检索接口：传 query + top_k + 可选 kb_id，
拿到「最相关的知识片段 + 出处元数据」，再综合成带引用的回答。

**契约（字段名一经确认不再更改，本包先做内部服务，不做 HTTP）**：

输入（未来 HTTP body）:
    {
      "query": "明渠均匀流形成条件",
      "top_k": 5,
      "kb_id": 3            # 可选；缺省跨全库
    }

输出:
    {
      "results": [
        {
          "text": "明渠均匀流的形成条件包括：……",   # 片段正文（含章节前缀）
          "score": 0.93,                            # 相关度（0~1，越高越相关）
          "source": {
            "document_name": "水力学.pdf",          # 来源文件名
            "document_type": "textbook",            # textbook/standard/manual/other
            "section": "7.4 明渠均匀流",            # 章节路径
            "page": 215,                            # 页码
            "clause_no": null,                      # 条款号（如 7.4.2）
            "formula_no": null,                     # 公式编号（如 7.4.3-1）
            "block_type": "text",                   # text/table/formula/figure
            "doc_id": 5,
            "chunk_id": 1882
          }
        }
      ]
    }

本服务只依赖 `rag.retrieve`（真正的检索逻辑），把结果翻译成契约结构。QA/搜索预览/
未来 HTTP 都走这里，保证对外格式永远一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.session import async_session_factory
from app.services import rag


@dataclass
class RetrievalSource:
    """每条结果的出处元数据（契约中的 source）。字段名一经确认不再更改。"""

    document_name: str
    document_type: str = "other"
    section: str | None = None
    page: int | None = None
    clause_no: str | None = None
    formula_no: str | None = None
    block_type: str = "text"
    doc_id: int | None = None
    chunk_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """转 dict（输出契约）；None 保留 null。"""
        return {
            "document_name": self.document_name,
            "document_type": self.document_type,
            "section": self.section,
            "page": self.page,
            "clause_no": self.clause_no,
            "formula_no": self.formula_no,
            "block_type": self.block_type,
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
        }


@dataclass
class RetrievalResult:
    """单条检索结果（契约中 results 的一项）。"""

    text: str
    score: float
    source: RetrievalSource

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "score": self.score, "source": self.source.to_dict()}


def _source_from_chunk(chunk: rag.RetrievedChunk) -> RetrievalSource:
    """从 rag.RetrievedChunk 组装契约 source（字段映射，保持结构稳定）。"""
    return RetrievalSource(
        document_name=chunk.source,
        document_type=chunk.doc_type,
        section=chunk.section,
        page=chunk.page,
        clause_no=chunk.clause_no,
        formula_no=chunk.formula_no,
        block_type=chunk.block_type,
        doc_id=chunk.doc_id,
        chunk_id=chunk.chunk_id,
    )


async def retrieve(
    query: str,
    top_k: int = 5,
    kb_id: int | None = None,
    doc_ids: list[int] | None = None,
) -> list[RetrievalResult]:
    """对外检索：query → 相关片段 + 出处（稳定契约）。

    - top_k：返回条数（默认 5，与现有问答一致）
    - kb_id：限定知识库（未来 DSH 可传）；缺省跨全库
    - doc_ids：限定文档（内部/未来扩展用）
    """
    async with async_session_factory() as db:
        chunks = await rag.retrieve(
            db,
            query,
            kb_id=kb_id,
            doc_ids=doc_ids,
            top_k=top_k,
            include_snippet=True,
        )
    return [
        RetrievalResult(
            text=chunk.snippet,
            score=chunk.score,
            source=_source_from_chunk(chunk),
        )
        for chunk in chunks
    ]


def results_to_dict(results: list[RetrievalResult]) -> dict[str, Any]:
    """契约完整 dict（未来 HTTP 响应的 body 直接 JSON 序列化）。"""
    return {"results": [r.to_dict() for r in results]}
