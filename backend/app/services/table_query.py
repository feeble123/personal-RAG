"""结构化读表服务（单元二 2-3）：按 table_id 聚合 Chunk.table_data，按列取值/筛选/计数。

背景：表格结构已由 2-1（解析层）、2-2（切片穿透 + 落库）存进
`Chunk.table_data = {table_id, columns, rows, row_index}`。本服务把散在多个 chunk 里的
同一张表聚合回完整视图，并提供按列操作的原语，供 2-4 意图识别后的精确通道使用。

查询是**精确读**（直接按列名/值匹配），不走向量/BM25——这是「看懂表格列」后
"列出来/数一下/筛出来" 类问题不再靠向量猜的关键。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document


def _norm_col(name: str) -> str:
    """列名归一：去全部空白（「名 称」→「名称」、「备 注」→「备注」）。"""
    return re.sub(r"\s+", "", (name or "").strip())


def _norm_cell(value) -> str:
    """单元格值归一：去首尾空白 + 折叠内部空白（值匹配时容空格差异）。"""
    return re.sub(r"\s+", "", str(value or "").strip())


@dataclass(frozen=True)
class TableView:
    """一张表的完整结构化视图（列名 + 数据行，跨块聚合后）。

    数据行已对齐列宽（每行长度 == 列数），不含表头。所有按列操作都先做列名归一化，
    容「名 称」这类带空格的表头与查询里的「名称」对齐。
    """

    table_id: str
    columns: list[str]
    rows: list[list[str]]

    def column_index(self, column: str) -> int | None:
        target = _norm_col(column)
        for i, c in enumerate(self.columns):
            if _norm_col(c) == target:
                return i
        return None

    def column_values(self, column: str) -> list[str]:
        """取一整列的值（按行顺序，含重复）。"""
        idx = self.column_index(column)
        if idx is None:
            return []
        return [row[idx] for row in self.rows]

    def unique_values(self, column: str) -> list[str]:
        """取一整列的去重值（保持首次出现顺序）。"""
        seen: set[str] = set()
        out: list[str] = []
        for v in self.column_values(column):
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def filter_rows(self, column: str, value) -> list[list[str]]:
        """筛选：column 列的值 == value 的所有行（值归一化后比较）。"""
        idx = self.column_index(column)
        if idx is None:
            return []
        target = _norm_cell(value)
        return [row for row in self.rows if _norm_cell(row[idx]) == target]

    def count(self, column: str, value) -> int:
        """计数：column 列的值 == value 的行数。"""
        return len(self.filter_rows(column, value))

    def lookup(self, where_column: str, where_value, get_column: str) -> list[str]:
        """查值：where_column == where_value 的行里取 get_column 的值（可能多行）。

        例：lookup("名称", "动力配电箱", "数量") → ["3"]。
        """
        gi = self.column_index(get_column)
        if gi is None:
            return []
        return [row[gi] for row in self.filter_rows(where_column, where_value)]

    def filter_date_after(self, column: str, iso_date: str) -> list[list[str]]:
        """日期筛选：column 列值（ISO YYYY-MM-DD）> iso_date 的行。

        依赖 2-3 前置（单元二③）已把日期统一成 YYYY-MM-DD——ISO 串的字典序即时间序，
        无需解析日期对象。
        """
        idx = self.column_index(column)
        if idx is None:
            return []
        return [row for row in self.rows if _norm_cell(row[idx]) > iso_date]


def _aggregate(table_id: str, chunks: list[Chunk]) -> TableView | None:
    """把同一 table_id 的多个 chunk 拼回完整表（按 row_index 排序，对齐列宽）。

    当前一张表通常就是一个 chunk（整表行全量），此聚合为未来大表拆块预留：
    拆块后各行按 row_index 拼回、列宽对齐，查询逻辑不变。
    """
    columns: list[str] | None = None
    parts: list[tuple[int, list[list[str]]]] = []
    for c in chunks:
        td = c.table_data
        if not td:
            continue
        cols = td.get("columns") or []
        rows = td.get("rows") or []
        if columns is None:
            columns = list(cols)
        parts.append((int(td.get("row_index", 0) or 0), rows))
    if columns is None:
        return None
    parts.sort(key=lambda x: x[0])
    width = len(columns)
    merged: list[list[str]] = []
    for _, rows in parts:
        for r in rows:
            r = list(r)
            if len(r) < width:
                r = r + [""] * (width - len(r))
            merged.append(r[:width])
    return TableView(table_id=table_id, columns=columns, rows=merged)


async def load_table(
    db: AsyncSession, table_id: str, kb_id: int | None = None
) -> TableView | None:
    """按 table_id 聚合 active 版本的所有表格切片 → TableView（找不到返回 None）。"""
    stmt = select(Chunk).where(Chunk.table_data.is_not(None))
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    # active 过滤：只读当前发布版本的切片（retired 不可作为数据源）
    active_stmt = select(Document.active_version_id).where(Document.active_version_id.is_not(None))
    if kb_id is not None:
        active_stmt = active_stmt.where(Document.kb_id == kb_id)
    active_ids = {r[0] for r in (await db.execute(active_stmt)).all()}
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    chunks = (await db.scalars(stmt)).all()
    matched = [c for c in chunks if (c.table_data or {}).get("table_id") == table_id]
    if not matched:
        return None
    return _aggregate(table_id, matched)


async def list_table_ids(db: AsyncSession, kb_id: int | None = None) -> list[str]:
    """列出库里所有表格的 table_id（去重，供 2-4 找表用）。"""
    stmt = select(Chunk).where(Chunk.table_data.is_not(None))
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    active_stmt = select(Document.active_version_id).where(Document.active_version_id.is_not(None))
    if kb_id is not None:
        active_stmt = active_stmt.where(Document.kb_id == kb_id)
    active_ids = {r[0] for r in (await db.execute(active_stmt)).all()}
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    chunks = (await db.scalars(stmt)).all()
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        tid = (c.table_data or {}).get("table_id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out
