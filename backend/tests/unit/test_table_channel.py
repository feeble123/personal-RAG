"""单元二 2-4：意图识别读表问题 + 精确读表通道（强门禁，找不到就回退）。

覆盖：
- table_query_kind / is_table_query：计数（数量/几台）→ count、枚举（有哪些/列出）→ enum、
  非读表问题 → None
- 精确通道纯函数：内容词抽取（jieba 剔除框架词）、表匹配分、计数答案、枚举答案
- 强门禁：非表格问题（预警分级几级 / 应急预案措施）在真实库里分数不够 → 返回 None（回退向量）
"""
from __future__ import annotations

import pytest

from app.services.intent import is_table_query, table_query_kind
from app.services.table_query import (
    TableView,
    _answer_count,
    _answer_enum,
    _content_words,
    _table_score,
    query_table,
)


def _device_tv() -> TableView:
    """四方井设备表（真实列名「名 称」带空格）。"""
    return TableView(
        table_id="doc11:excel-0",
        columns=["序号", "名 称", "型号规格", "单位", "数量", "备 注"],
        rows=[
            ["1", "动力配电箱", "", "台", "3", ""],
            ["2", "消防动力配电箱", "", "台", "2", ""],
            ["3", "照明配电箱", "PZ30", "台", "6", ""],
        ],
        section="四方井仓库及管理用房主要电气设备材料表.xlsx / Sheet1",
        source="四方井仓库及管理用房主要电气设备材料表.xlsx",
        chunk_ids=(1,),
        doc_id=11,
        kb_id=10,
    )


class TestIntentTableQuery:
    def test_count_signals(self):
        assert table_query_kind("动力配电箱的数量是多少？") == "count"
        assert table_query_kind("水泵有几台？") == "count"
        assert table_query_kind("闸门一共多少套？") == "count"

    def test_enum_signals(self):
        assert table_query_kind("四方井仓库有哪些电气设备？") == "enum"
        assert table_query_kind("请列出所有方案名称") == "enum"

    def test_non_table_questions(self):
        assert table_query_kind("明渠均匀流的形成条件是什么？") is None
        assert table_query_kind("什么是径流？") is None

    def test_is_table_query_alias(self):
        assert is_table_query("动力配电箱的数量是多少？") is True
        assert is_table_query("什么是径流？") is False


class TestPreciseChannel:
    def test_content_words_strip_framework(self):
        words = _content_words("动力配电箱的数量是多少？")
        # 「动力/配电箱」留下，「数量/多少/的/是」被剔除
        assert "数量" not in words
        assert any("配电箱" in w for w in words) or "动力配电箱" in words

    def test_table_score_columns_and_cells(self):
        tv = _device_tv()
        assert _table_score(["动力", "配电箱"], tv) >= 3.0  # 单元格值命中 + 可能列名

    def test_sheet_name_outweighs_row_count(self):
        """用户点名 sheet（制度体系）时，4 行小表必须压过 36 行大表。

        两个表同文档、同文件名，只有 sheet 名不同：查询里「制度/体系」只命中小表
        的 sheet 名，大表靠 36 行单元格值命中「方案」也压不过 sheet 名强信号。
        """
        small = TableView(
            table_id="doc10:excel-0", columns=["序号", "方案名称"],
            rows=[["1", "质量保证体系"], ["2", "结构实体检测方案"]],
            section="已报送方案台账.xlsx / 制度体系",
            source="已报送方案台账.xlsx",
        )
        big = TableView(
            table_id="doc10:excel-1", columns=["序号", "方案名称"],
            rows=[["1", f"方案{i}"] for i in range(36)],
            section="已报送方案台账.xlsx / 备案版方案台账",
            source="已报送方案台账.xlsx",
        )
        words = _content_words("已报送方案台账中，制度体系有哪些方案？")
        assert _table_score(words, small) > _table_score(words, big)

    def test_entity_gate_returns_none_when_named_entity_absent(self):
        """点名实体（肖家湾水厂）在表里毫无落点时，精确通道不 dump 整列冒充答案。

        「肖家湾水厂项目有哪些报送方案」里的「肖家湾」既不在列名/单元格值，也不在
        sheet 名/文件名——这张表答不了这个具体项目，应回退向量检索。
        """
        from app.services.table_query import _word_present

        tv = TableView(
            table_id="doc10:excel-1", columns=["序号", "方案名称"],
            rows=[["1", "临时用电施工组织设计"], ["2", "临建方案"]],
            section="已报送方案台账.xlsx / 备案版方案台账",
            source="已报送方案台账.xlsx",
        )
        words = _content_words("肖家湾水厂项目有哪些报送方案？")
        # 「肖家湾」≥3 字、非泛词，且表里无落点 → 门禁应判回退
        assert any(len(w) >= 3 and w == "肖家湾" for w in words)
        assert not _word_present("肖家湾", tv)

    def test_answer_count_reads_number_column(self):
        tv = _device_tv()
        ans = _answer_count(tv, "动力配电箱的数量是多少？", _content_words("动力配电箱的数量是多少？"))
        assert ans is not None
        assert ans.kind == "count"
        assert ans.answer_text == "动力配电箱 数量: 3"

    def test_answer_count_does_not_bleed_into_similar_name(self):
        """「消防动力配电箱」不能混进「动力配电箱」的计数。"""
        tv = _device_tv()
        ans = _answer_count(tv, "动力配电箱的数量是多少？", _content_words("动力配电箱的数量是多少？"))
        assert ans is not None
        assert len(ans.rows) == 1
        assert ans.rows[0][1] == "动力配电箱"

    def test_answer_enum_lists_name_column(self):
        tv = _device_tv()
        ans = _answer_enum(tv, "四方井仓库有哪些电气设备？", _content_words("四方井仓库有哪些电气设备？"))
        assert ans is not None
        assert ans.kind == "enum"
        assert "动力配电箱" in ans.answer_text
        assert "照明配电箱" in ans.answer_text

    def test_enum_does_not_filter_on_generic_word(self):
        """「方案/措施」这类泛词不当作过滤条件，否则整列被过滤掉。"""
        tv = TableView(
            table_id="t", columns=["方案名称", "状态"],
            rows=[["质量保证体系", "已归档"], ["结构实体检测方案", "已归档"]],
        )
        # 「制度体系有哪些方案」里的「方案」是泛词，不应过滤掉整列
        ans = _answer_enum(tv, "有哪些方案？", ["方案"])
        assert ans is not None
        assert "质量保证体系" in ans.answer_text


class TestQueryTableGate:
    """强门禁：非表格问题在「无表库」里必须返回 None（不接管，回退向量）。"""

    pytestmark = pytest.mark.asyncio

    async def test_non_table_query_returns_none_on_tableless_kb(self, client):
        """库里没有任何 table_data（纯 PDF 库）→ 精确通道返回 None。"""
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            ans = await query_table(db, "预警信息分为哪几个等级？")
            assert ans is None

    async def test_table_query_falls_back_when_no_match(self, client):
        """即便出现「几级」计数词，库里表与问题无交集 → None（不猜）。"""
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            ans = await query_table(db, "应急响应分为几级？")
            assert ans is None
