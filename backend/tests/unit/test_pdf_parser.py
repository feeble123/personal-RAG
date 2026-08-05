"""PDF 解析器单元测试：文字层样本（tests/data/shuili.pdf）+ 扫描样本判定。"""
from __future__ import annotations

from pathlib import Path

from app.services.chunker import chunk_blocks
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
