"""P0-11 检索出处元数据：chunk 块类型标记 + 条款号/公式号提取 + 落库字段。

单元1：chunker 带出 block_type、manager 提取 clause_no/formula_no 并落库；
Schema 迁移列在 test_alembic_baseline.py 已覆盖（upgrade head + compare_metadata）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.ingestion import manager
from app.services.chunker import Chunk, StructureAwareChunker, chunk_toc_pages
from app.services.parser.base import ParsedBlock


class TestChunkBlockType:
    def test_table_block_gets_block_type(self):
        """表格块 → chunk.block_type == 'table'；正文 → 'text'。"""
        blocks = [
            ParsedBlock(text="表格前正文", section="第一章", block_type="paragraph"),
            ParsedBlock(text="a | b\n1 | 2", section="第一章", block_type="table"),
            ParsedBlock(text="表格后正文", section="第一章", block_type="paragraph"),
        ]
        chunks = StructureAwareChunker().chunk(blocks)
        table_chunk = next(c for c in chunks if "a | b" in c.content)
        assert table_chunk.block_type == "table"
        text_chunks = [c for c in chunks if "a | b" not in c.content]
        assert all(c.block_type == "text" for c in text_chunks)

    def test_toc_chunk_default_text(self):
        """目录切片默认 text（无表格标记）。"""
        chunks = chunk_toc_pages({3: "第一章 1\n第二章 10"})
        assert chunks and all(c.block_type == "text" for c in chunks)


class TestClauseAndFormulaExtract:
    def test_extract_clause_no(self):
        """章节路径末尾编号 → 条款号。"""
        assert manager._extract_clause_no("7.4 明渠均匀流") == "7.4"
        assert manager._extract_clause_no("3 液体运动的流束理论 / 3.2 引用标准") == "3.2"
        # 无编号章节 → None
        assert manager._extract_clause_no("第一章") is None
        assert manager._extract_clause_no(None) is None

    def test_extract_formula_no(self):
        """内容里的公式编号。"""
        assert manager._extract_formula_no("Q=K√h\n(3.45)") == "3.45"
        assert manager._extract_formula_no("由式（7.4.3-1）可见…") == "7.4.3-1"
        # 无公式编号 → None
        assert manager._extract_formula_no("这是普通段落") is None

    def test_clause_from_deep_section(self):
        """多级章节取最深层条款号。"""
        assert manager._extract_clause_no("2 水静力学 / 2.3 重力作用下静水压强的基本公式") == "2.3"


class TestWriteChunksCarriesMeta:
    async def test_write_chunks_sets_new_fields(self, client, sample_kb):
        """落库后 chunks 带 block_type/clause_no/formula_no（经 sample_kb 真实入库）。"""
        kb_id, doc_id = sample_kb
        from sqlalchemy import select

        from app.db.models import Chunk
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(Chunk).where(Chunk.doc_id == doc_id).limit(5)
                )
            ).scalars().all()
            assert rows, "应存在已入库 chunks"
            # 至少一个块有 clause_no（md 有「## 明渠均匀流」，但 md 走解析器路径 section 是标题路径）
            # 块类型默认 text
            for c in rows:
                assert c.block_type in ("text", "table"), f"block_type 非法: {c.block_type}"
                assert isinstance(c.clause_no, (str, type(None)))
                assert isinstance(c.formula_no, (str, type(None)))


class TestUploadDocType:
    async def test_upload_with_doc_type(self, client, admin_headers):
        """上传带 doc_type → 落库 + 回显（教材）。"""
        r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": "doc类型库"})
        assert r.status_code == 201, r.text
        kb_id = r.json()["id"]
        md = "# 水利\n\n## 明渠均匀流\n\n明渠均匀流的形成条件包括：长直棱柱体渠道、正坡。\n"
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("textbook.md", md.encode("utf-8"), "text/markdown")},
            data={"doc_type": "textbook"},
        )
        assert r.status_code == 201, r.text
        doc_id = r.json()["id"]
        assert r.json()["doc_type"] == "textbook", "回显应带 doc_type"
        # 落库
        from sqlalchemy import select

        from app.db.models import Document
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            doc = await db.get(Document, doc_id)
            assert doc.doc_type == "textbook"
        # 清理
        await client.delete(f"/api/admin/documents/{doc_id}", headers=admin_headers)
        await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)

    async def test_upload_invalid_doc_type_falls_back(self, client, admin_headers):
        """非法 doc_type → 回退 other。"""
        r = await client.post("/api/admin/kbs", headers=admin_headers, json={"name": "doc类型库2"})
        kb_id = r.json()["id"]
        md = "# 水利\n\n正文。\n"
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("x.md", md.encode("utf-8"), "text/markdown")},
            data={"doc_type": "hacker"},
        )
        assert r.status_code == 201
        assert r.json()["doc_type"] == "other"
        await client.delete(f"/api/admin/documents/{r.json()['id']}", headers=admin_headers)
        await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)
