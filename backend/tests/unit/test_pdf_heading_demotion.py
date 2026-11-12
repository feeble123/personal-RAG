"""扫描件参数表：表格单元格被误判成标题时，内容会被挤出切片。

真实案例：某论文第 42 页的参数表，每个单元格单独成行且字号偏大，
被 detect_heading 判成 heading；parent-child 切分中标题不进正文，
导致「43.99 / 0.30」这类数值在知识库里根本检索不到。

修复：整页标题数异常（>8）时，把「数值开头」或「非编号」的标题降级为段落。
"""
from __future__ import annotations

from app.services.parser.base import ParsedBlock
from app.services.parser.pdf import _MAX_HEADINGS_PER_PAGE, _demote_table_like_headings


def _heading(text: str) -> ParsedBlock:
    return ParsedBlock(text=text, page=42, block_type="heading")


def _table_page() -> list[ParsedBlock]:
    rows = ["0.30 表层自由水蓄水容量", "43.99 表层自由水蓄水容量", "53.59 张力水蓄水容量曲线指数",
            "0.40 张力水蓄水容量曲线指数", "0.23 壤中流消退系数", "0.67 壤中流出流系数",
            "0.99 地下水消退系数", "1.35 表层自由水蓄水容量曲线指数", "0.80 蒸散发折算系数"]
    return [_heading(r) for r in rows] + [_heading("4.2 最优预报结果")]


def test_table_cells_are_demoted():
    assert len(_table_page()) > _MAX_HEADINGS_PER_PAGE
    out = _demote_table_like_headings(_table_page())
    assert all(b.block_type == "paragraph" for b in out[:-1])
    # 真正的章节标题保留
    assert out[-1].block_type == "heading"


def test_normal_page_untouched():
    """正常正文页（标题数没超阈值）一律不动。"""
    blocks = [_heading("4.2 最优预报结果"), ParsedBlock(text="正文一段。", page=42)]
    out = _demote_table_like_headings(blocks)
    assert out[0].block_type == "heading"


def test_non_numbered_headings_also_demoted():
    """字号判定的标题（无编号）在异常页上同样降级。"""
    blocks = [_heading(f"单元格{i}") for i in range(10)] + [_heading("4.2 最优预报结果")]
    out = _demote_table_like_headings(blocks)
    assert out[0].block_type == "paragraph"
    assert out[-1].block_type == "heading"


def test_section_heading_with_one_decimal_kept():
    """「4.2」这类一位小数的章节号不能被当成数值。"""
    blocks = [_heading(f"{i}.1 某章节") for i in range(1, 10)] + [_heading("4.2 最优预报结果")]
    out = _demote_table_like_headings(blocks)
    assert out[-1].block_type == "heading"
