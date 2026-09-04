"""P1-4 单元4：parent-child 切片。

覆盖：
- build_parent_child：原子分块、子块/父块 token 预算、覆盖性（所有原子至少属 1 子块+1 父块）
- 写库两遍插入：父块先插、子块回填 parent_chunk_id + parent_context
- 检索注入：命中子块（偏短）注入父上下文
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import async_session_factory
from app.modules.ingestion import manager
from app.services.chunking.parent_child import (
    ParentChildChunk,
    _count_tokens,
    build_parent_child,
)
from app.services.parser.base import ParsedBlock
from app.services.parser.ir import DocumentElement, ElementType

_cnt = {"n": 0}


def _mk_blocks(contents: list[tuple[str, str]]) -> list[ParsedBlock]:
    """构造 blocks：[(text, block_type)]，全部同章节。"""
    return [
        ParsedBlock(text=t, block_type=bt, section="第一章 / 1.1 节", page=1)
        for t, bt in contents
    ]


class TestBuildParentChild:
    def test_all_atoms_covered(self):
        blocks = _mk_blocks(
            [
                ("这是第一段内容，介绍水力学基本概念。", "paragraph"),
                ("第二段继续阐述原理。", "paragraph"),
                ("第三段补充公式推导。", "paragraph"),
            ]
        )
        result = build_parent_child([], blocks=blocks)
        assert result, "应产出 parent-child 切片"
        # 所有子块非空 + 有父块；子块内容应包含于父块（小段落组时子块=父块内容）
        for pc in result:
            assert pc.content.strip(), "子块不应为空"
            assert pc.parent_content.strip(), "父块不应为空"
            # 子块正文（去 breadcrumb）是父块正文的子集
            child_body = pc.content.split("\n", 1)[-1]
            parent_body = pc.parent_content.split("\n", 1)[-1]
            assert child_body in parent_body, "子块内容应包含于父块"

    def test_tokens_within_budget(self):
        """子块 token 在 200-500 范围；父块 ≤1600。"""
        blocks = _mk_blocks([("内容" * 50 + f" 段落{i}。", "paragraph") for i in range(30)])
        result = build_parent_child([], blocks=blocks)
        for pc in result:
            ct = _count_tokens(pc.content)
            # 允许 20% 容差（tiktoken 中英文混排）
            assert ct <= 500 * 1.2, f"子块超限: {ct}"
        # 父块不超 1600*1.2
        for pc in result:
            pt = _count_tokens(pc.parent_content)
            assert pt <= 1600 * 1.2, f"父块超限: {pt}"

    def test_table_rows_grouped(self):
        """表格行不按字符跨行切：整组作为子块+父块。"""
        rows = [f"名称{i} | 数值{i} | 单位" for i in range(5)]
        blocks = _mk_blocks([(r, "table") for r in rows])
        result = build_parent_child([], blocks=blocks)
        assert result
        # 表格父块包含所有行
        first = result[0]
        assert all(row.split("|")[0].strip() in first.parent_content for row in rows)

    def test_mixed_table_text_not_contaminated(self):
        """单元 D：正文与表格同小节，正文子块不得被连坐标成 table。

        回归背景：旧逻辑 `block_type="table" if any(a["type"]=="table" for a in group)`
        使「小节里只要有一个表格，整节正文子块全被标 table」——水力学 324 个 table、
        前100页 180 个 table 中大量是正文段落被误标。
        """
        blocks = _mk_blocks(
            [
                ("这是表格前的正文段落，介绍水力学基本概念，内容较长以占据一个子块。", "paragraph"),
                ("表格行一 | 数值一 | 单位", "table"),
                ("表格行二 | 数值二 | 单位", "table"),
                ("这是表格后的另一段正文，说明表格中参数的含义。", "paragraph"),
            ]
        )
        result = build_parent_child([], blocks=blocks)
        assert result
        # 表格子块（含表格行）标 table；正文子块（不含表格行）标 text，不能被连坐成 table
        seen_table = False
        for pc in result:
            body = pc.content.split("\n", 1)[-1]
            if "表格行" in body:
                seen_table = True
                assert pc.block_type == "table", (
                    f"表格子块被误标为 {pc.block_type}：{body[:40]!r}"
                )
            else:
                assert pc.block_type == "text", (
                    f"正文子块被误标为 {pc.block_type}：{body[:40]!r}"
                )
        assert seen_table, "应有表格子块被单独切出"

    def test_pure_table_block_is_table(self):
        """单元 D：纯表格子块仍标 table。"""
        rows = [f"名称{i} | 数值{i} | 单位" for i in range(5)]
        blocks = _mk_blocks([(r, "table") for r in rows])
        result = build_parent_child([], blocks=blocks)
        assert result
        assert all(pc.block_type == "table" for pc in result), "纯表格块应标 table"


class TestTableDataPenetration:
    """单元二 2-2：表格结构化数据穿透切片（element.table → ParentChildChunk.table_data）。"""

    def _table_elements(self):
        """构造两个 TABLE element（结构完整），夹一个正文段落。"""
        return [
            DocumentElement(
                element_id="mineru-0",
                type=ElementType.TABLE,
                text="表1 参数\n参数 | 取值\n糙率 | 0.025\n流速 | 2.5m/s",
                reading_order=0,
                table={
                    "rows": [
                        ["参数", "取值"],
                        ["糙率", "0.025"],
                        ["流速", "2.5m/s"],
                    ],
                    "header_path": ["参数", "取值"],
                },
            ),
            DocumentElement(
                element_id="mineru-1",
                type=ElementType.PARAGRAPH,
                text="这是表格后的正文说明。",
                reading_order=1,
            ),
        ]

    def test_table_element_produces_table_data(self):
        """纯表格 element → 子块 table_data 带列名 + 数据行（不含表头）+ table_id。"""
        elements = self._table_elements()
        pc = build_parent_child(elements, blocks=[])
        table_chunks = [c for c in pc if c.block_type == "table"]
        assert table_chunks, "应有纯表格子块"
        for tc in table_chunks:
            assert tc.table_data is not None, "表格子块应携带 table_data"
            assert tc.table_data["table_id"] == "mineru-0"
            assert tc.table_data["columns"] == ["参数", "取值"]
            assert tc.table_data["rows"] == [["糙率", "0.025"], ["流速", "2.5m/s"]]
            assert tc.table_data["row_index"] == 0

    def test_non_table_chunk_has_no_table_data(self):
        """正文子块 table_data = None（不被连坐）。"""
        elements = self._table_elements()
        pc = build_parent_child(elements, blocks=[])
        text_chunks = [c for c in pc if c.block_type == "text"]
        assert text_chunks, "应有正文子块"
        assert all(c.table_data is None for c in text_chunks), "正文子块不得携带 table_data"

    def test_no_table_data_from_blocks_compat(self):
        """blocks 兼容层（无 table 结构）→ 所有子块 table_data=None，不崩。"""
        blocks = _mk_blocks([("名称 | 数值", "table"), ("甲 | 1", "table")])
        result = build_parent_child([], blocks=blocks)
        assert result
        assert all(c.table_data is None for c in result)


class TestWriteChunksParentChild:
    pytestmark = pytest.mark.asyncio

    async def test_two_pass_insert_with_parent(self, client):
        """写库：父块先插、子块回填 parent_chunk_id + parent_context。"""
        _cnt["n"] += 1
        n = _cnt["n"]
        kb_id, doc_id = None, None
        async with async_session_factory() as db:
            kb = KnowledgeBase(name=f"pc库{n}", status="ready")
            db.add(kb)
            await db.flush()
            doc = Document(
                kb_id=kb.id, filename=f"pc{n}.md", stored_path=f"pc{n}.md",
                file_type="md", status="pending",
            )
            db.add(doc)
            await db.commit()
            kb_id, doc_id = kb.id, doc.id

        # 用最小 DocumentVersion + ParentChildChunk 模拟写库
        pc_list = [
            ParentChildChunk(
                content="## 第一章 / 1.1 节\n子块内容",
                parent_content="## 第一章 / 1.1 节\n父块内容包含子块",
                section="第一章 / 1.1 节", page=1,
                child_hash="a" * 64, parent_hash="b" * 64,
            ),
            ParentChildChunk(
                content="## 第一章 / 1.1 节\n子块二",
                parent_content="## 第一章 / 1.1 节\n父块内容包含子块二",
                section="第一章 / 1.1 节", page=1,
                child_hash="c" * 64, parent_hash="d" * 64,
            ),
        ]
        try:
            from app.db.models import DocumentVersion

            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                ver = DocumentVersion(document_id=doc_id, status="building")
                db.add(ver)
                await db.commit()
                await manager._write_chunks(db, doc, ver, pc_list)
                await db.commit()

                # 验证：2 父块 + 2 子块
                chunks = (await db.scalars(
                    select(Chunk).where(Chunk.document_version_id == ver.id)
                )).all()
                parents = [c for c in chunks if c.block_type == "parent"]
                children = [c for c in chunks if c.block_type == "text"]
                assert len(parents) == 2, f"应 2 父块, 实得 {len(parents)}"
                assert len(children) == 2, f"应 2 子块, 实得 {len(children)}"
                # 子块回填 parent
                for child in children:
                    assert child.parent_chunk_id is not None, "子块应回填 parent_chunk_id"
                    assert child.parent_context, "子块应存 parent_context"
                # 父块 parent_chunk_id 为空
                for parent in parents:
                    assert parent.parent_chunk_id is None
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
                await db.commit()


    async def test_write_chunks_persists_table_data(self, client):
        """单元二 2-2：落库时子块 table_data 正确写入，父块不携带（避免重复计数）。"""
        _cnt["n"] += 1
        n = _cnt["n"]
        kb_id, doc_id = None, None
        async with async_session_factory() as db:
            kb = KnowledgeBase(name=f"表库{n}", status="ready")
            db.add(kb)
            await db.flush()
            doc = Document(
                kb_id=kb.id, filename=f"tab{n}.xlsx", stored_path=f"tab{n}.xlsx",
                file_type="xlsx", status="pending",
            )
            db.add(doc)
            await db.commit()
            kb_id, doc_id = kb.id, doc.id

        table_data = {
            "table_id": "excel-0",
            "columns": ["序号", "方案名称", "完成时间"],
            "rows": [["1", "质量保证体系", "2025-09-15"]],
            "row_index": 0,
        }
        pc_list = [
            ParentChildChunk(
                content="## 台账\n序号 | 方案名称 | 完成时间\n1 | 质量保证体系 | 2025-09-15",
                parent_content="## 台账\n序号 | 方案名称 | 完成时间\n1 | 质量保证体系 | 2025-09-15",
                section="台账", page=None,
                child_hash="t" * 64, parent_hash="u" * 64,
                block_type="table", table_data=table_data,
            ),
        ]
        try:
            from app.db.models import DocumentVersion

            async with async_session_factory() as db:
                doc = await db.get(Document, doc_id)
                ver = DocumentVersion(document_id=doc_id, status="building")
                db.add(ver)
                await db.commit()
                await manager._write_chunks(db, doc, ver, pc_list)
                await db.commit()

                chunks = (await db.scalars(
                    select(Chunk).where(Chunk.document_version_id == ver.id)
                )).all()
                children = [c for c in chunks if c.block_type == "table"]
                parents = [c for c in chunks if c.block_type == "parent"]
                assert len(children) == 1, f"应 1 表格子块, 实得 {len(children)}"
                assert children[0].table_data == table_data, "子块 table_data 应正确落库"
                assert all(p.table_data is None for p in parents), "父块不携带 table_data"
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
                await db.commit()


class TestRetrieveParentInjection:
    pytestmark = pytest.mark.asyncio

    async def test_hydrate_injects_parent_context(self, client, sample_kb):
        """命中偏短子块 → 注入父上下文（有 parent_context 的 chunk）。"""
        from app.db.session import async_session_factory
        from app.services import rag

        kb_id, _ = sample_kb
        # 给 sample_kb 造一个带 parent_context 的 chunk，模拟 parent-child 数据
        async with async_session_factory() as db:
            from sqlalchemy import select

            chunk = (await db.scalars(
                select(Chunk).where(Chunk.kb_id == kb_id).limit(1)
            )).one()
            # 插一个父块，子块引用它（父上下文要够长 > min_content_len=40）
            parent_context = (
                "## 完整父块\n这是父上下文，用于补充短子块信息。"
                "明渠均匀流形成条件包括长直棱柱体渠道、正坡、糙率不变、流量恒定，"
                "这是水力学中的重要概念。"
            )
            parent = Chunk(
                kb_id=kb_id, doc_id=chunk.doc_id,
                document_version_id=chunk.document_version_id,
                chunk_index=9999, content=parent_context,
                content_hash="p" * 64, block_type="parent",
            )
            db.add(parent)
            await db.flush()
            # 模拟 parent-child：子块内容改短 + 挂父块
            chunk.content = "明渠均匀流条件"  # 偏短（< min_content_len*2）
            chunk.parent_context = parent_context
            chunk.parent_chunk_id = parent.id
            await db.commit()

            # 直接调 _hydrate：偏短子块应注入 parent_context
            ranked = [(chunk.id, 0.9)]
            items = await rag._hydrate(db, ranked, include_snippet=True)
            assert items, "应 hydrate 出结果"
            assert "父上下文" in items[0].snippet, "短子块应注入父上下文"
