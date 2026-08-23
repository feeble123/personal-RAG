"""P1-2 单元B：bake-off 指标纯函数测试（不需真跑 MinerU）。"""
from __future__ import annotations

import pytest

from evaluation.scripts.benchmark_metrics import (
    assert_same_sample_pages,
    build_route_table,
    compute_metrics,
    weighted_score,
)


class TestComputeMetrics:
    def test_counts_chars(self):
        """中文文本字符量正确（去空白）。"""
        m = compute_metrics("明渠均匀流 的形成条件\n包括")
        assert m["total_chars"] == 12  # 去空格/换行后 12 个字符

    def test_garble_ratio_detected(self):
        """含 � 替换符 → 乱码率 > 0。"""
        m = compute_metrics("正常中文 � 乱码")
        assert m["garble_ratio"] > 0

    def test_clause_refs_extracted(self):
        """条款号 8.2.3 被提取。"""
        m = compute_metrics("按 8.2.3 条执行")
        assert "8.2.3" in m["clause_refs"]

    def test_gt_similarity(self):
        """文字层 GT 相似率在 [0,1]。"""
        m = compute_metrics("明渠均匀流条件", ground_truth="明渠均匀流形成条件")
        assert 0 <= m["gt_similarity"] <= 1


class TestWeightedScore:
    def test_score_range(self):
        """加权评分有界 [0,100]。"""
        s = weighted_score({"total_chars": 1000, "clause_count": 5, "garble_ratio": 0.01,
                            "chinese_common_ratio": 0.5, "gt_similarity": 0.9})
        assert 0 <= s <= 100

    def test_bad_quality_low_score(self):
        """高乱码率 → 低分。"""
        good = weighted_score({"total_chars": 2000, "clause_count": 10, "garble_ratio": 0.0,
                               "chinese_common_ratio": 0.5})
        bad = weighted_score({"total_chars": 2000, "clause_count": 10, "garble_ratio": 0.5,
                              "chinese_common_ratio": 0.1})
        assert bad < good

    def test_time_penalty(self):
        """耗时越久分越低（相同质量下）。"""
        fast = weighted_score({"total_chars": 1000, "clause_count": 5, "garble_ratio": 0.0,
                               "chinese_common_ratio": 0.5}, time_cost_s=5)
        slow = weighted_score({"total_chars": 1000, "clause_count": 5, "garble_ratio": 0.0,
                               "chinese_common_ratio": 0.5}, time_cost_s=300)
        assert slow < fast


class TestRouteTable:
    def test_route_prefers_higher_score(self):
        """同文档类型两引擎，推荐分数高的。"""
        reports = [
            {"doc_type": "scanned_standard", "engine": "rapid", "score": 55, "pages": 3, "est_time_s": 10},
            {"doc_type": "scanned_standard", "engine": "mineru", "score": 80, "pages": 3, "est_time_s": 120},
        ]
        table = build_route_table(reports)
        assert table["scanned_standard"]["recommended"] == "mineru"

    def test_route_text_layer_prefers_fast(self):
        """文字层：RapidOCR 分数相近时看路由（这里 mineru 分低则 rapid）。"""
        reports = [
            {"doc_type": "text_layer", "engine": "rapid", "score": 90, "pages": 5, "est_time_s": 3},
            {"doc_type": "text_layer", "engine": "mineru", "score": 70, "pages": 5, "est_time_s": 200},
        ]
        table = build_route_table(reports)
        assert table["text_layer"]["recommended"] == "rapid"


class TestSameSamplePages:
    def test_mismatch_raises(self):
        """两引擎页数不一致 → 报错（bake-off 必须同页集）。"""
        samples = [
            {"doc": "a.pdf", "engine": "rapid", "pages": [1, 2, 3]},
            {"doc": "a.pdf", "engine": "mineru", "pages": [1, 2]},
        ]
        with pytest.raises(ValueError):
            assert_same_sample_pages(samples)

    def test_match_ok(self):
        """页数一致 → 不抛。"""
        samples = [
            {"doc": "a.pdf", "engine": "rapid", "pages": [1, 2, 3]},
            {"doc": "a.pdf", "engine": "mineru", "pages": [1, 2, 3]},
        ]
        assert_same_sample_pages(samples)  # 不抛
