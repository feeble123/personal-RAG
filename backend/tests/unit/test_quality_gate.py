"""P1-3 单元E：质量门禁测试（多特征评分 + garble 修复）。"""
from __future__ import annotations

import pytest

from app.services.parser.quality import (
    QualityPolicy,
    compute_quality_score,
    is_review_required,
)


class TestComputeScore:
    def test_healthy_doc_high_score(self):
        """正常文档（文字层、够长、无乱码）→ 高分，不 needs_review。"""
        q = {"pages": 50, "text_pages": 50, "ocr_pages": 0, "total_chars": 8000,
             "garble_ratio": 0.0, "tables": 5, "mean_ocr_confidence": None}
        score, reasons = compute_quality_score(q)
        assert score > 80
        assert not is_review_required(score, reasons)

    def test_garble_ratio_accumulated(self):
        """含 � 的文本 → garble_ratio > 0 → 触发乱码 reason。"""
        q = {"pages": 10, "text_pages": 10, "ocr_pages": 0, "total_chars": 2000,
             "garble_ratio": 0.08, "tables": 0}
        score, reasons = compute_quality_score(q)
        assert any("乱码率过高" in r for r in reasons)

    def test_pseudo_high_confidence_caught(self):
        """伪高置信：置信 0.95 但表格字符占比超高 → 触发表格异常。"""
        q = {"pages": 10, "text_pages": 0, "ocr_pages": 10, "total_chars": 6000,
             "garble_ratio": 0.0, "tables": 20, "mean_ocr_confidence": 0.95,
             "ocr_confidence": [0.95] * 10, "table_chars": 5000}
        score, reasons = compute_quality_score(q)
        assert any("表格字符占比过高" in r for r in reasons)
        # 伪高置信应被捕获（表格异常导致 score 下降 + needs_review）
        assert is_review_required(score, reasons)

    def test_low_conf_pages_caught(self):
        """低置信页占比高 → 触发 reason。"""
        q = {"pages": 10, "text_pages": 0, "ocr_pages": 10, "total_chars": 5000,
             "garble_ratio": 0.0, "tables": 2, "mean_ocr_confidence": 0.3,
             "ocr_confidence": [0.3] * 8 + [0.9] * 2}
        score, reasons = compute_quality_score(q)
        assert any("OCR 平均置信度低" in r for r in reasons)

    def test_score_range(self):
        """score 有界 [0,100]。"""
        for q in [
            {"pages": 1, "text_pages": 1, "ocr_pages": 0, "total_chars": 100, "garble_ratio": 0.5, "tables": 0},
            {"pages": 100, "text_pages": 100, "ocr_pages": 0, "total_chars": 99999, "garble_ratio": 0.0, "tables": 50},
        ]:
            score, _ = compute_quality_score(q)
            assert 0 <= score <= 100

    def test_low_chars_review(self):
        """文本量过少 → needs_review。"""
        q = {"pages": 5, "text_pages": 5, "ocr_pages": 0, "total_chars": 100, "garble_ratio": 0.0, "tables": 0}
        score, reasons = compute_quality_score(q)
        assert any("文本量过少" in r for r in reasons)
        assert is_review_required(score, reasons)

    def test_review_reasons_preserved_format(self):
        """reasons 是字符串列表，可拼接（沿用现有 review_reasons 字段风格）。"""
        q = {"pages": 10, "text_pages": 0, "ocr_pages": 10, "total_chars": 100,
             "garble_ratio": 0.1, "tables": 0}
        _, reasons = compute_quality_score(q)
        assert isinstance(reasons, list)
        assert all(isinstance(r, str) for r in reasons)
        assert "; ".join(reasons)  # 可拼接到 review_reasons
