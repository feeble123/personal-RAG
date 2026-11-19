"""切片 → MinerU 块的对齐规则（`services/bbox_match.py`）。

为什么值得单测：回填是**一次性**的维护脚本，写错了不会有第二次机会——
把框标到隔壁段落上，用户会以为那句话在那里。所以规则要能被断言住。
"""
from __future__ import annotations

from app.services.bbox_match import (
    BlockRect,
    block_text,
    iter_lines,
    normalize,
    pick_block,
    score_block,
    shingles,
    unions,
)

R_TEXT = (10.0, 20.0, 200.0, 60.0)
R_TITLE = (10.0, 5.0, 120.0, 18.0)
R_TABLE = (14.0, 60.0, 300.0, 240.0)

CHUNK = "## 2 流域产流 / 2.3 流域蒸发\n在本章里我们讨论蒸发折算系数的问题，它反映水面与陆面蒸发的差异以及土壤含水量的影响。"


def test_normalize_strips_markdown_and_whitespace():
    assert normalize("## 2 流域产流 | K_c") == "2流域产流K_c"


def test_shingles_short_text():
    assert shingles("abc") == {"abc"}
    assert shingles("") == set()


def test_iter_lines_handles_both_shapes():
    """普通块的行挂在块上；表格块的行嵌在 blocks 下。只认一种就会取到空文本。"""
    plain = {"type": "text", "lines": [{"spans": [{"content": "正文"}]}]}
    assert [h.get("type") for h in iter_lines(plain)] == ["text"]
    table = {
        "type": "table",
        "blocks": [
            {"type": "table_caption", "lines": [{"spans": [{"content": "表5.2"}]}]},
            {"type": "table_body", "lines": [{"spans": [{"content": ""}]}]},
        ],
    }
    assert [h.get("type") for h in iter_lines(table)] == ["table_caption", "table_body"]


def test_block_text_uses_caption_for_tables():
    assert block_text("table", "正文不该用", "表5.2 参数表") == "表5.2参数表"
    assert block_text("text", " 正文 内容 ", "") == "正文内容"


def test_text_block_contained_in_chunk_scores_full():
    block = BlockRect("text", R_TEXT, normalize("在本章里我们讨论蒸发折算系数的问题"))
    assert score_block(block, CHUNK, chunk_is_table=False) == 1.0


def test_title_block_is_not_chosen_for_long_chunk():
    """实测踩过：正文切片被判到了「2.3.3 水面蒸发量估计」这种小标题上。"""
    blocks = [
        BlockRect("title", R_TITLE, normalize("2.3.3水面蒸发量估计")),
        BlockRect("text", R_TEXT, normalize("在本章里我们讨论蒸发折算系数的问题，它反映水面与陆面蒸发的差异")),
    ]
    picked = pick_block(blocks, CHUNK, chunk_is_table=False)
    assert picked is not None
    assert picked.kind == "text"
    assert picked.rect == R_TEXT


def test_table_matched_by_caption():
    blocks = [BlockRect("table", R_TABLE, normalize("表1.2灾害统计"))]
    chunk = "## 1 绪论 / 1.2 水文预报作用 / 表1.2 灾害统计\n年份 | 灾害类型\n1906 | 洪灾"
    picked = pick_block(blocks, chunk, chunk_is_table=True)
    assert picked is not None
    assert picked.rect == R_TABLE


def test_single_table_on_page_is_accepted_without_caption():
    """扫描件的表格切片常常没有表题，页上只有一张表时认它。"""
    blocks = [
        BlockRect("text", R_TEXT, normalize("一些正文内容，和这张表没关系")),
        BlockRect("table", R_TABLE, ""),
    ]
    picked = pick_block(blocks, "年份 | 灾害类型 | 1906 | 洪灾", chunk_is_table=True)
    assert picked is not None
    assert picked.kind == "table"


def test_no_candidate_returns_none():
    blocks = [BlockRect("text", R_TEXT, normalize("完全无关的一段文字，长度也够"))]
    assert pick_block(blocks, "另一种完全不同的内容，和它没有共同片段", chunk_is_table=False) is None
    assert pick_block([], CHUNK, chunk_is_table=False) is None


def test_unions_merges_caption_and_body():
    assert unions([(10, 20, 100, 40), (10, 5, 90, 18)]) == [[10, 5, 100, 40]]
