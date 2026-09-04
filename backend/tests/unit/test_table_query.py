"""单元二 2-3：结构化读表服务。

覆盖：
- TableView 按列取值 / 去重 / 筛选 / 计数 / 查值 / 日期筛选（纯函数层）
- 列名归一（「名 称」对齐「名称」）；值归一（空格差异）
- load_table 聚合 active 版本切片（DB 层，跨块拼回整表）
- 列宽对齐（数据行缺列补空 / 多列截断）
"""
from __future__ import annotations

import pytest

from app.services.table_query import (
    TableView,
    _aggregate,
    load_table,
)


def _tv(columns, rows):
    return TableView(table_id="t1", columns=columns, rows=rows)


class TestTableViewPure:
    def test_column_values(self):
        t = _tv(["序号", "方案名称", "完成时间"], [["1", "质量保证体系", "2025-09-15"]])
        assert t.column_values("方案名称") == ["质量保证体系"]

    def test_column_name_normalized(self):
        """「名 称」列名对齐「名称」查询（源文件列名带空格）。"""
        t = _tv(["序号", "名 称", "单位", "数量"], [["1", "动力配电箱", "台", "3"]])
        assert t.column_index("名称") == 1
        assert t.column_values("名称") == ["动力配电箱"]

    def test_unique_values_keep_order(self):
        t = _tv(["类别"], [["甲"], ["乙"], ["甲"], ["丙"]])
        assert t.unique_values("类别") == ["甲", "乙", "丙"]

    def test_filter_rows_value_normalized(self):
        """单元格值空格差异不影响匹配（「动力 配电箱」≈「动力配电箱」）。"""
        t = _tv(["名称", "数量"], [["动力配电箱", "3"], ["消防动力配电箱", "2"]])
        assert t.filter_rows("名称", "动力配电箱") == [["动力配电箱", "3"]]

    def test_count(self):
        t = _tv(["名称"], [["动力配电箱"], ["动力配电箱"], ["照明配电箱"]])
        assert t.count("名称", "动力配电箱") == 2

    def test_lookup(self):
        t = _tv(["名称", "数量"], [["动力配电箱", "3"], ["照明配电箱", "6"]])
        assert t.lookup("名称", "动力配电箱", "数量") == ["3"]

    def test_lookup_missing_column(self):
        t = _tv(["名称"], [["甲"]])
        assert t.lookup("名称", "甲", "不存在的列") == []
        assert t.column_values("不存在的列") == []

    def test_filter_date_after(self):
        """日期筛选（ISO 字典序即时间序）。"""
        t = _tv(
            ["方案名称", "完成时间"],
            [["A", "2025-09-15"], ["B", "2025-09-20"], ["C", "2025-10-01"]],
        )
        assert t.filter_date_after("完成时间", "2025-09-16") == [
            ["B", "2025-09-20"], ["C", "2025-10-01"],
        ]


class TestAggregate:
    def test_single_chunk_full_table(self):
        class _C:
            table_data = {
                "table_id": "t1",
                "columns": ["名称", "数量"],
                "rows": [["动力配电箱", "3"]],
                "row_index": 0,
            }

        tv = _aggregate("t1", [_C()])
        assert tv is not None
        assert tv.columns == ["名称", "数量"]
        assert tv.rows == [["动力配电箱", "3"]]

    def test_multi_chunk_merged_by_row_index(self):
        """大表拆块后按 row_index 拼回完整表（为未来预留）。"""
        class _C:
            def __init__(self, td):
                self.table_data = td

        c1 = _C({"table_id": "t1", "columns": ["名称", "数量"], "rows": [["甲", "1"]], "row_index": 0})
        c2 = _C({"table_id": "t1", "columns": ["名称", "数量"], "rows": [["乙", "2"], ["丙", "3"]], "row_index": 1})
        tv = _aggregate("t1", [c2, c1])  # 乱序传入，按 row_index 排序
        assert tv.rows == [["甲", "1"], ["乙", "2"], ["丙", "3"]]

    def test_row_width_aligned(self):
        """缺列补空、多列截断，行对齐列宽。"""
        class _C:
            table_data = {"table_id": "t1", "columns": ["a", "b", "c"], "rows": [["x"], ["1", "2", "3", "4"]], "row_index": 0}

        tv = _aggregate("t1", [_C()])
        assert tv.rows == [["x", "", ""], ["1", "2", "3"]]


class TestLoadTable:
    pytestmark = pytest.mark.asyncio

    async def test_load_table_aggregates_active(self, client):
        """DB 层：按 table_id 聚合 active 版本切片。"""
        from sqlalchemy import delete

        from app.db.models import Chunk, Document, DocumentVersion, KnowledgeBase
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            kb = KnowledgeBase(name="读表库", status="ready")
            db.add(kb)
            await db.flush()
            doc = Document(kb_id=kb.id, filename="t.xlsx", stored_path="t.xlsx", file_type="xlsx", status="ready")
            db.add(doc)
            await db.commit()
            ver = DocumentVersion(document_id=doc.id, status="active")
            db.add(ver)
            await db.commit()
            doc.active_version_id = ver.id
            await db.commit()

            db.add(Chunk(
                kb_id=kb.id, doc_id=doc.id, document_version_id=ver.id, chunk_index=0,
                content="名称 | 数量\n动力配电箱 | 3", content_hash="a" * 64,
                block_type="table",
                table_data={"table_id": f"doc{doc.id}:excel-0", "columns": ["名称", "数量"],
                            "rows": [["动力配电箱", "3"]], "row_index": 0},
            ))
            await db.commit()

            tv = await load_table(db, f"doc{doc.id}:excel-0", kb_id=kb.id)
            assert tv is not None
            assert tv.column_values("名称") == ["动力配电箱"]
            assert tv.count("名称", "动力配电箱") == 1

            # 找不到的表 → None
            assert await load_table(db, f"doc{doc.id}:nonexistent", kb_id=kb.id) is None

            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb.id))
            await db.commit()
