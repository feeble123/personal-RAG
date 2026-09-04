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
    _answer_total,
    _content_words,
    _question_core,
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


def _ledger_views() -> tuple[TableView, TableView]:
    """一份台账两个 sheet：制度体系(4 行) + 备案版方案台账(36 行)，同 doc_id。"""
    small = TableView(
        table_id="doc10:excel-0", columns=["序号", "方案名称"],
        rows=[["1", f"制度方案{i}"] for i in range(4)],
        section="已报送方案台账.xlsx / 制度体系",
        source="已报送方案台账.xlsx", doc_id=10,
    )
    big = TableView(
        table_id="doc10:excel-1", columns=["序号", "方案名称"],
        rows=[["1", f"备案方案{i}"] for i in range(36)],
        section="已报送方案台账.xlsx / 备案版方案台账",
        source="已报送方案台账.xlsx", doc_id=10,
    )
    return small, big


class TestTotalCount:
    """单元二 2-4 修复：「一共多少 X」求总数（区分于点名实体计数）。"""

    def test_question_core_drops_output_meta(self):
        """「请以表格输出」「不用把交底时间输出」是输出格式，不是问句本体。"""
        q = "这份台账中一共有多少方案？请以表格的形式输出给我。不用把交底的时间输出给我，我不关心交底时间。"
        core = _question_core(q)
        assert "一共" in core and "多少" in core and "方案" in core
        assert "交底" not in core and "时间" not in core and "表格" not in core

    def test_content_words_after_core_drop_meta_instruction(self):
        """问句本体清洗后，「交底/时间」这类领域词不混进内容词（不再虚高分带偏表选择）。"""
        q = "这份台账中一共有多少方案？请以表格的形式输出给我。不用把交底的时间输出给我"
        words = _content_words(_question_core(q))
        assert "方案" in words
        assert "交底" not in words
        assert "时间" not in words

    def test_total_sums_across_sheets(self):
        """整份台账（未点名 sheet）→ 两个 sheet 行数求和 = 40。"""
        small, big = _ledger_views()
        words = _content_words(_question_core("这份台账中一共有多少方案？"))
        ans = _answer_total([small, big], "这份台账中一共有多少方案？", words)
        assert ans is not None
        assert ans.kind == "count"
        assert "40" in ans.answer_text

    def test_total_scoped_to_named_sheet(self):
        """点名 sheet（制度体系）→ 只数那张表 = 4。"""
        small, big = _ledger_views()
        words = _content_words(_question_core("制度体系一共有多少方案？"))
        ans = _answer_total([small, big], "制度体系一共有多少方案？", words)
        assert ans is not None
        assert "4" in ans.answer_text and "40" not in ans.answer_text

    def test_container_word_does_not_scope_sheet(self):
        """容器词「台账」指整份文件，不能当「备案版方案台账」的 sheet 定位 → 仍是 40。"""
        small, big = _ledger_views()
        words = _content_words(_question_core("这份台账中一共有多少方案？"))
        ans = _answer_total([small, big], "这份台账中一共有多少方案？", words)
        assert ans is not None
        assert "40" in ans.answer_text

    def test_unit_word_not_treated_as_subject(self):
        """「方案」是单位不是实体：_answer_count 不能把「方案」当主体去锁「临建方案」。"""
        tv = TableView(
            table_id="t", columns=["序号", "方案名称"],
            rows=[["1", "临建方案"], ["2", "施工组织设计"]],
        )
        # 只有「方案」一词（无具体实体）→ _answer_count 应拒绝（返回 None），走求总数
        ans = _answer_count(tv, "一共有多少方案？", ["方案"])
        assert ans is None

    def test_column_name_hit_not_repeated(self):
        """一个词命中再多列也只 +3 一次（「时间」命中 5 个「××时间」列不虚高）。"""
        tv = TableView(
            table_id="t",
            columns=["审批时间", "报送时间", "交底时间", "完成时间", "归档时间"],
            rows=[["2025-09-01", "2025-09-07", "2025-09-23", "", ""]],
        )
        # 「时间」命中 5 个列名，去重后应只 +3（不是 15）
        assert _table_score(["时间"], tv) == 3.0


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
