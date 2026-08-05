"""PDF 解析辅助函数单元测试：标题检测 / OCR 判定 / 乱码统计。"""
from __future__ import annotations

from app.core.config import settings
from app.services.parser.pdf import (
    _count_text_chars,
    _garble_ratio,
    _is_heading,
    _page_needs_ocr,
    _clean_text,
)


class TestHeadingDetection:
    def test_chapter_pattern(self):
        assert _is_heading("第三章 明渠恒定流", 12, 12) is True

    def test_numbered_section_pattern(self):
        assert _is_heading("3.1 明渠均匀流的形成条件", 12, 12) is True

    def test_chinese_number_list_pattern(self):
        assert _is_heading("一、概述", 12, 12) is True
        assert _is_heading("（一）总则", 12, 12) is True

    def test_larger_font_heading(self):
        # 字号显著大于正文且行短 → 视为标题
        assert _is_heading("大字号标题", 18, 12) is True

    def test_normal_paragraph_not_heading(self):
        assert _is_heading("这是一段很长的正文内容，说明明渠均匀流的相关特性。", 12, 12) is False

    def test_empty_or_too_long(self):
        assert _is_heading("", 12, 12) is False
        assert _is_heading("很" * 70, 18, 12) is False  # 超长不判标题


class TestQualityDetection:
    def test_empty_text_needs_ocr(self):
        assert _count_text_chars("   \n  ") == 0

    def test_garble_ratio(self):
        # 替换字符 � 计入乱码
        assert _garble_ratio("正常文本") < 0.01
        assert _garble_ratio("���") > 0.9
        assert _garble_ratio("") == 0.0

    def test_clean_text_filters_page_numbers(self):
        cleaned = _clean_text("标题\n\n12\n第 3 页\n正文内容")
        assert "12" not in cleaned
        assert "第 3 页" not in cleaned
        assert "标题" in cleaned and "正文内容" in cleaned

    def test_ocr_decision_threshold(self, monkeypatch):
        """文本量低于阈值 → 判定扫描页需 OCR；足够文本 → 文字层。"""
        monkeypatch.setattr(settings, "pdf_text_threshold_per_page", 40)

        class FakePage:
            def get_text(self, _t):
                return ""

        assert _page_needs_ocr(FakePage()) is True

        class FakePage2:
            def get_text(self, _t):
                return "明渠均匀流是指水流沿程不变的流动。" * 5

        assert _page_needs_ocr(FakePage2()) is False

        class FakePage3:  # 有文本但乱码率高 → OCR
            def get_text(self, _t):
                return "�" * 100

        assert _page_needs_ocr(FakePage3()) is True
