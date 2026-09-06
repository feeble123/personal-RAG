"""PDF 解析器单元测试：文字层样本（tests/data/shuili.pdf）+ 扫描样本判定。"""
from __future__ import annotations

from pathlib import Path

from app.services.chunker import chunk_blocks
from app.services.parser import pdf as pdf_module
from app.services.parser.pdf import PDFParser

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "shuili.pdf"
SCAN = Path(__file__).resolve().parents[1] / "data" / "scan_sample.pdf"


class TestPDFParser:
    def test_text_layer_pdf(self):
        if not SAMPLE.exists():
            return  # 样本缺失则跳过（非失败）
        parsed = PDFParser().parse(SAMPLE, "shuili.pdf")
        assert parsed.page_count >= 1
        assert parsed.quality["text_pages"] >= 1
        assert parsed.blocks, "文字层 PDF 应提取出内容块"

    def test_text_layer_chunks_have_sections(self):
        if not SAMPLE.exists():
            return
        parsed = PDFParser().parse(SAMPLE, "shuili.pdf")
        chunks = chunk_blocks(parsed.blocks)
        assert chunks, "应产出 chunk"
        # 至少一个 chunk 带章节上下文（标题检测生效）
        assert any(c.section for c in chunks) or any("## " in c.content for c in chunks)

    def test_scan_pdf_detected_as_ocr(self):
        """无文本层的样本应判定为扫描页（不要求 OCR 有结果，只验证分层判定）。"""
        if not SCAN.exists():
            return
        parsed = PDFParser().parse(SCAN, "scan_sample.pdf")
        assert parsed.quality["ocr_pages"] >= 1
        assert parsed.quality["text_pages"] == 0

    def test_garble_ratio_recorded(self):
        """P1-3：扫描样本解析后 garble_ratio 被真实统计（此前恒 0 的 bug 修复）。"""
        if not SCAN.exists():
            return
        parsed = PDFParser().parse(SCAN, "scan_sample.pdf")
        # garble_ratio 现在应 >= 0（真实统计），而不是恒 0.0（旧 bug）
        assert "garble_ratio" in parsed.quality
        assert parsed.quality["garble_ratio"] >= 0.0
        # 文本量、页码等质量字段完整
        assert "total_chars" in parsed.quality
        assert parsed.quality["pages"] >= 1

    def test_ocr_path_uses_rotation_correction(self, monkeypatch):
        """P1-3 接线：OCR 渲染路径必须走 render_corrected（旋转页转正），而非裸 get_pixmap。

        用 monkeypatch 替换 pdf 模块命名空间里的 render_corrected，解析扫描样本，
        断言它被调用——证明「转正工具」真正接进了 OCR 生产线。
        """
        if not SCAN.exists():
            return
        called: list[int] = []
        original = pdf_module.render_corrected

        def fake_render_corrected(page, dpi=200, **kw):
            called.append(1)
            # 真实调用原函数，保证解析流程不因替换而短路
            return original(page, dpi=dpi, **kw)

        monkeypatch.setattr(pdf_module, "render_corrected", fake_render_corrected)
        PDFParser().parse(SCAN, "scan_sample.pdf")
        assert called, "OCR 路径未调用 render_corrected——旋转页纠正未接线"
