"""原文文件端点（P-原文查看 U1）：签名 URL / Range / 文件缺失 / 路径穿越。

为什么这些用例必须存在：
- 签名 URL 是**免登录**的，它是整个系统里唯一一个"带凭证的匿名下载口"，
  验签写错就是越权下载任意文档；
- `stored_path` 来自数据库，一旦被写脏就是路径穿越读文件；
- pdf.js 靠 206 分段拉大书，Range 坏了表现为"大文件打不开"，很难查。
"""
from __future__ import annotations

import time

import pytest

from app.modules.knowledge import source as src


@pytest.mark.asyncio
async def test_file_token_then_download(client, admin_headers, sample_kb):
    _kb_id, doc_id = sample_kb
    r = await client.post(f"/api/documents/{doc_id}/file-token", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["expires_in"] == src.TOKEN_TTL
    assert body["url"].startswith(f"/api/documents/{doc_id}/file?t=")

    # 匿名拉取：不带 Authorization 头也必须成功（pdf.js 带不了）
    r = await client.get(body["url"])
    assert r.status_code == 200, r.text
    assert "明渠均匀流" in r.content.decode("utf-8")
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_range_request_returns_partial(client, admin_headers, sample_kb):
    _kb_id, doc_id = sample_kb
    url = (await client.post(f"/api/documents/{doc_id}/file-token", headers=admin_headers)).json()["url"]
    r = await client.get(url, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206, r.text
    assert len(r.content) == 10
    assert r.headers["content-range"].startswith("bytes 0-9/")


@pytest.mark.asyncio
async def test_head_probe_is_supported(client, admin_headers, sample_kb):
    """浏览器内置预览/部分阅读器先发 HEAD 探长度；405 会被当成"文件不可用"。"""
    _kb_id, doc_id = sample_kb
    url = (await client.post(f"/api/documents/{doc_id}/file-token", headers=admin_headers)).json()["url"]
    r = await client.head(url)
    assert r.status_code == 200, r.text
    assert int(r.headers["content-length"]) > 0
    assert r.headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_tampered_token_is_rejected(client, admin_headers, sample_kb):
    _kb_id, doc_id = sample_kb
    url = (await client.post(f"/api/documents/{doc_id}/file-token", headers=admin_headers)).json()["url"]
    # 改签名末位
    bad = url[:-1] + ("0" if url[-1] != "0" else "1")
    r = await client.get(bad)
    assert r.status_code == 403
    assert r.json()["code"] == "SOURCE_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_token_bound_to_document(client, admin_headers, sample_kb):
    """拿 A 文档的签名去读 B 文档必须失败（签名里绑了 doc_id）。"""
    _kb_id, doc_id = sample_kb
    url = (await client.post(f"/api/documents/{doc_id}/file-token", headers=admin_headers)).json()["url"]
    token = url.split("t=", 1)[1]
    r = await client.get(f"/api/documents/{doc_id + 999}/file?t={token}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_expired_token_is_rejected(client, admin_headers, sample_kb):
    _kb_id, doc_id = sample_kb
    expired = src.issue_token(doc_id, user_id=1, ttl=-10)  # 已过期
    r = await client.get(f"/api/documents/{doc_id}/file?t={expired}")
    assert r.status_code == 403
    assert r.json()["code"] == "SOURCE_TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_unknown_document(client, admin_headers):
    r = await client.post("/api/documents/999999/file-token", headers=admin_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_missing_file_on_disk(client, admin_headers, sample_kb):
    """库搬了、uploads 没搬 —— 最常见的事故，必须给 410 和明确文案。"""
    from app.core.config import settings

    _kb_id, doc_id = sample_kb
    url = (await client.post(f"/api/documents/{doc_id}/file-token", headers=admin_headers)).json()["url"]
    token = url.split("t=", 1)[1]

    from sqlalchemy import select

    from app.db.models import Document
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        doc = (await db.scalars(select(Document).where(Document.id == doc_id))).one()
        stored_path = doc.stored_path
    target = settings.upload_dir_path / stored_path
    backup = target.with_suffix(target.suffix + ".bak")
    target.rename(backup)
    try:
        r = await client.get(f"/api/documents/{doc_id}/file?t={token}")
        assert r.status_code == 410
        assert "uploads" in r.json()["message"]
    finally:
        backup.rename(target)


def test_resolve_rejects_path_traversal():
    class _Doc:
        stored_path = "../../../../windows/win.ini"

    with pytest.raises(Exception) as exc:
        src.resolve_source_path(_Doc())  # type: ignore[arg-type]
    assert "路径" in str(exc.value)


def test_token_ttl_is_short():
    """有效期是这套免登录下载口的唯一防线，别被人随手调大。"""
    assert 60 <= src.TOKEN_TTL <= 900


def test_issue_and_verify_roundtrip():
    token = src.issue_token(doc_id=7, user_id=3, ttl=60)
    src.verify_token(7, token)
    with pytest.raises(Exception):
        src.verify_token(8, token)
    assert int(token.split(".")[0]) > int(time.time())


# ---------- Excel 原文（表格视图） ----------


def test_read_sheets_reads_values_and_trims_blank_tail(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "台账"
    ws.append(["序号", "方案名称", "数量"])
    ws.append([1, "质量保证体系", 3.0])
    ws.append([None, None, None])  # 尾部空行应被裁掉
    path = tmp_path / "a.xlsx"
    wb.save(path)

    from app.services.source_office import read_sheets

    sheets = read_sheets(path)
    assert len(sheets) == 1
    assert sheets[0].name == "台账"
    # 1.0 → "1"，跟用户在 Excel 里看到的一致
    assert sheets[0].rows[1] == ["1", "质量保证体系", "3"]
    assert len(sheets[0].rows) == 2


def test_read_sheets_marks_truncated(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    for i in range(20):
        ws.append([i])
    path = tmp_path / "b.xlsx"
    wb.save(path)

    from app.services.source_office import read_sheets

    sheets = read_sheets(path, max_rows=5)
    assert len(sheets[0].rows) == 5
    assert sheets[0].truncated is True


@pytest.mark.asyncio
async def test_sheets_endpoint_rejects_non_excel(client, sample_kb):
    _kb_id, doc_id = sample_kb  # .md
    token = src.issue_token(doc_id, user_id=1, ttl=300)
    r = await client.get(f"/api/documents/{doc_id}/sheets", params={"t": token})
    assert r.status_code == 415
    assert r.json()["code"] == "SOURCE_SHEETS_UNSUPPORTED"
