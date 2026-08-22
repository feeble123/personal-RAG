"""P0-10 单元1：上传内容校验（magic number + zip bomb + 文本安全）。

覆盖：
- 伪造扩展名：内容不是 %PDF 的 .pdf → 400 CONTENT_MISMATCH
- 真 PDF（%PDF 开头）→ 201（内容校验通过）
- 假 docx（内容不是 zip）→ 400 CONTENT_MISMATCH
- 二进制伪装成 md（含 NUL 字节）→ 400 BINARY_TEXT
- 真 md → 201
- zip bomb：压缩比异常 → 拒绝（upload_guard 单测直接验证）
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.core.exceptions import BizError
from app.modules.knowledge.upload_guard import verify_file


class TestUploadGuardUnit:
    """upload_guard 直接单测（无需 HTTP/DB）。"""

    def test_fake_pdf_rejected(self, tmp_path):
        p = tmp_path / "evil.pdf"
        p.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00")  # 实际是 PE 可执行文件头
        with pytest.raises(BizError) as e:
            verify_file("pdf", p)
        assert e.value.code == "CONTENT_MISMATCH"

    def test_real_pdf_accepted(self, tmp_path):
        p = tmp_path / "ok.pdf"
        p.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n%%EOF")
        verify_file("pdf", p)  # 不抛错即通过

    def test_fake_docx_rejected(self, tmp_path):
        p = tmp_path / "evil.docx"
        p.write_bytes(b"NOTAZIPFILE" * 10)  # 不是 zip 容器
        with pytest.raises(BizError) as e:
            verify_file("docx", p)
        assert e.value.code == "CONTENT_MISMATCH"

    def test_real_docx_accepted(self, tmp_path):
        p = tmp_path / "ok.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>")
        verify_file("docx", p)

    def test_binary_pretending_md_rejected(self, tmp_path):
        p = tmp_path / "evil.md"
        p.write_bytes(b"# heading\n\x00\x00\x00binary payload")  # 含 NUL
        with pytest.raises(BizError) as e:
            verify_file("md", p)
        assert e.value.code == "BINARY_TEXT"

    def test_text_md_accepted(self, tmp_path):
        p = tmp_path / "ok.md"
        p.write_bytes("# 水利工程基础\n\n明渠均匀流条件。".encode("utf-8"))
        verify_file("md", p)

    def test_csv_formula_not_blocked_by_guard(self, tmp_path):
        """CSV 公式注入（=cmd）由下游处理，guard 只校验是否文本。"""
        p = tmp_path / "ok.csv"
        p.write_bytes(b"a,b\n=cmd|' /C calc'!A0,2\n")
        verify_file("csv", p)  # 是文本即通过（公式清洗属于解析层）

    def test_zip_bomb_ratio_rejected(self, tmp_path):
        """压缩比 > 500 倍 → 拒绝。"""
        p = tmp_path / "bomb.xlsx"
        # 高度重复内容压缩比极大：500KB 全 A → deflate 压到 <1KB → 比远超 500 倍
        payload = b"A" * (500 * 1024)
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", payload)
        # 校验压缩比：deflate 对全 A 压缩率 > 500 倍（500KB -> ~200 字节）
        with pytest.raises(BizError) as e:
            verify_file("xlsx", p)
        assert e.value.code in ("ZIP_BOMB",)


# ---------------- HTTP 层测试（走上传接口）----------------
class TestUploadApi:
    async def test_upload_fake_pdf_rejected(self, client, admin_headers, sample_kb):
        kb_id, _ = sample_kb
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("evil.pdf", b"MZ\x90\x00\x03\x00", "application/pdf")},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "CONTENT_MISMATCH"

    async def test_upload_real_pdf_accepted(self, client, admin_headers, sample_kb):
        kb_id, _ = sample_kb
        # 最小合法 PDF（%PDF 头 + 尾）——内容校验通过即 201（入库失败是另一路径）
        pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("real.pdf", pdf, "application/pdf")},
        )
        assert r.status_code == 201, r.text

    async def test_upload_fake_docx_rejected(self, client, admin_headers, sample_kb):
        kb_id, _ = sample_kb
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("evil.docx", b"NOTAZIP" * 20, "application/octet-stream")},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "CONTENT_MISMATCH"

    async def test_upload_binary_md_rejected(self, client, admin_headers, sample_kb):
        kb_id, _ = sample_kb
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("evil.md", b"# x\n\x00\x00binary", "text/markdown")},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "BINARY_TEXT"

    async def test_upload_text_md_accepted(self, client, admin_headers, sample_kb):
        kb_id, _ = sample_kb
        r = await client.post(
            f"/api/admin/kbs/{kb_id}/documents/upload",
            headers=admin_headers,
            files={"file": ("ok.md", "# 明渠\n\n均匀流条件。".encode(), "text/markdown")},
        )
        assert r.status_code == 201, r.text
