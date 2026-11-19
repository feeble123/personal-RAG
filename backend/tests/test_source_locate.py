"""原文定位（P-原文查看 U2）：文本层词框匹配 + 扫描件块级 bbox 兜底。

为什么必须测：
- 匹配口径一变，就会出现"前端高亮得出来、后端说找不到"的分裂；
- 扫描件那条路没有文本层可依赖，只能靠回填的 bbox，退化成"整页无框"时
  必须**明确说定位不到**，而不是随便指一个框；
- 页号来自历史数据，可能越界。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import source_locate as sl


def make_pdf(path: Path) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "alpha beta gamma", fontsize=14)
    page.insert_text((72, 130), "delta epsilon zeta", fontsize=14)
    blank = doc.new_page()  # 第二页：模拟扫描件（无文本层）
    blank.draw_rect(pymupdf.Rect(50, 60, 200, 120), color=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return path


def test_normalize_ignores_whitespace_and_pipes():
    assert sl.normalize("a b | c") == "abc"
    assert sl.normalize("深层张力水蓄水容量 | [35,80]") == "深层张力水蓄水容量[35,80]"


def test_text_layer_match_across_lines(tmp_path):
    path = make_pdf(tmp_path / "a.pdf")
    result = sl.locate_in_page(path, 1, quote="beta gamma delta")
    assert result.source == "text"
    assert result.has_text_layer is True
    assert len(result.rects) == 2, "跨行引用应压成两条行框"
    x0, y0, x1, y1 = result.rects[0]
    assert y1 < 130, "第一行框不该跑到第二行上去"
    assert x1 > x0 and y1 > y0


def test_pipes_in_quote_do_not_break_match(tmp_path):
    """引用常带 `a | b` 这种分隔符，原文里并没有竖线。"""
    path = make_pdf(tmp_path / "a.pdf")
    result = sl.locate_in_page(path, 1, quote="alpha | beta")
    assert result.source == "text"
    assert result.rects


def test_markdown_marks_are_ignored_on_both_sides(tmp_path):
    """切片里的 `## 标题` / `* 注` 在原文里没有井号星号；两边必须用同一把尺子。

    这条用例是踩出来的：一开始只把*引用*里的记号去掉，正文里的 `*` 没去掉，
    结果原文带星号的那几条反而从"能定位"退化成了"定位不到"。
    """
    path = make_pdf(tmp_path / "a.pdf")
    assert sl.locate_in_page(path, 1, quote="## alpha | beta").source == "text"
    assert sl.locate_in_page(path, 1, quote="alpha * beta").source == "text"


def test_quote_not_found_says_so(tmp_path):
    path = make_pdf(tmp_path / "a.pdf")
    result = sl.locate_in_page(path, 1, quote="完全不存在的一句话")
    assert result.source == "none"
    assert not result.rects
    assert "没在原文里找到" in (result.note or "")


def test_scanned_page_falls_back_to_bbox(tmp_path):
    path = make_pdf(tmp_path / "a.pdf")
    result = sl.locate_in_page(
        path, 2, quote="扫描件里当然找不到", chunk_bbox={"rects": [[50, 60, 200, 120]]}
    )
    assert result.source == "bbox"
    assert result.has_text_layer is False
    assert result.rects == [(50.0, 60.0, 200.0, 120.0)]
    assert "扫描件" in (result.note or "")


def test_scanned_page_without_bbox_is_none(tmp_path):
    path = make_pdf(tmp_path / "a.pdf")
    result = sl.locate_in_page(path, 2, quote="随便什么")
    assert result.source == "none"
    assert result.has_text_layer is False


def test_page_number_is_clamped(tmp_path):
    path = make_pdf(tmp_path / "a.pdf")
    high = sl.locate_in_page(path, 99, quote="alpha")
    assert high.page == high.page_count == 2
    low = sl.locate_in_page(path, 0, quote="alpha")
    assert low.page == 1


def test_bad_bbox_shape_is_ignored():
    assert sl.bbox_rects(None) == []
    assert sl.bbox_rects({"rects": "nope"}) == []
    assert sl.bbox_rects({"rects": [["a", "b"], [1, 2, 3]]}) == []
    assert sl.bbox_rects({"rects": [[1, 2, 3, 4]]}) == [(1.0, 2.0, 3.0, 4.0)]


# ---------- 接口层 ----------


def _tok(doc_id: int) -> str:
    from app.modules.knowledge.source import issue_token

    return issue_token(doc_id, user_id=1, ttl=300)


@pytest.mark.asyncio
async def test_locate_rejects_non_pdf(client, sample_kb):
    _kb_id, doc_id = sample_kb  # 示例文档是 .md
    r = await client.get(f"/api/documents/{doc_id}/locate", params={"t": _tok(doc_id), "page": 1})
    assert r.status_code == 415
    assert r.json()["code"] == "SOURCE_LOCATE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_locate_requires_valid_signature(client, sample_kb):
    """签名是这组接口唯一的凭证——坏了必须拒绝，不能退回"没登录也放行"。"""
    _kb_id, doc_id = sample_kb
    r = await client.get(f"/api/documents/{doc_id}/locate", params={"t": "1.1.deadbeef", "page": 1})
    assert r.status_code == 403
    assert r.json()["code"] == "SOURCE_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_locate_unknown_document(client):
    r = await client.get("/api/documents/999999/locate", params={"t": _tok(999999), "page": 1})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_locate_chunk_from_other_document(client, sample_kb):
    from sqlalchemy import select

    from app.db.models import Chunk
    from app.db.session import async_session_factory

    kb_id, doc_id = sample_kb
    async with async_session_factory() as db:
        chunk = (await db.scalars(select(Chunk).where(Chunk.doc_id == doc_id))).first()
    assert chunk is not None

    # 用别的 doc 去查这个 chunk → 必须拒绝，不能串文档
    other = doc_id + 999
    r = await client.get(
        f"/api/documents/{other}/locate", params={"t": _tok(other), "chunk_id": chunk.id}
    )
    assert r.status_code == 404
