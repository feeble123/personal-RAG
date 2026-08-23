"""P1-2 单元D：解析路由决策测试（纯函数，不真解析）。"""
from __future__ import annotations

from app.services.parser.router import (
    ENGINE_MINERU,
    ENGINE_RAPID,
    ENGINE_TEXT,
    RouterPolicy,
    route_pdf,
)


def _policy(**kw) -> RouterPolicy:
    base = RouterPolicy(mineru_enabled=False, scan_engine="rapid", mineru_min_scan_ratio=0.5, min_complex_pages=2)
    return base.__class__(**{**base.__dict__, **kw})


class TestRouteTextLayer:
    def test_text_layer_pages_to_text(self):
        """纯文字层 → 文字层路径。"""
        d = route_pdf(total_pages=10, scanned_pages=0)
        assert d.doc_level == ENGINE_TEXT
        assert not d.use_mineru


class TestRouteScannedDefault:
    def test_scanned_to_rapid_default(self):
        """扫描页默认 rapid（MinerU 未启用）。"""
        d = route_pdf(total_pages=10, scanned_pages=8)
        assert d.doc_level == ENGINE_RAPID
        assert not d.use_mineru

    def test_mineru_disabled_keeps_rapid(self):
        """mineru_enabled=False → 即使扫描占比高也不启用。"""
        d = route_pdf(total_pages=10, scanned_pages=8, policy=_policy(mineru_enabled=False))
        assert d.doc_level == ENGINE_RAPID


class TestRouteMineru:
    def test_scanned_to_mineru_when_enabled(self):
        """mineru_enabled + scan_engine=mineru + 占比达标 → MinerU。"""
        d = route_pdf(total_pages=10, scanned_pages=8, policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_MINERU
        assert d.use_mineru

    def test_mineru_threshold_not_met(self):
        """扫描占比不足 → 回退 rapid。"""
        d = route_pdf(total_pages=10, scanned_pages=3, policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_RAPID
        assert not d.use_mineru

    def test_mineru_too_few_pages(self):
        """扫描页太少（< min_scan_pages）→ 回退 rapid。"""
        d = route_pdf(total_pages=5, scanned_pages=1, policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_RAPID

    def test_auto_with_high_ratio_uses_mineru(self):
        """auto 引擎 + 扫描占比高 → MinerU。"""
        d = route_pdf(total_pages=10, scanned_pages=8, policy=_policy(mineru_enabled=True, scan_engine="auto"))
        assert d.doc_level == ENGINE_MINERU

    def test_auto_with_low_ratio_keeps_rapid(self):
        """auto 引擎 + 扫描占比低 → rapid。"""
        d = route_pdf(total_pages=10, scanned_pages=2, policy=_policy(mineru_enabled=True, scan_engine="auto"))
        assert d.doc_level == ENGINE_RAPID

    def test_doc_level_mineru_for_all_scanned(self):
        """整文档扫描 → 文档级 MinerU。"""
        d = route_pdf(total_pages=48, scanned_pages=48, policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_MINERU
        assert d.use_mineru
        assert any("48" in r for r in d.reasons)


class TestRouteComplexity:
    """P1-2 路由修正：公式/表格/图片复杂度信号 → MinerU。"""

    def test_formula_heavy_text_layer_uses_mineru(self):
        """有文字层但公式多（水力学场景）→ 走 MinerU（ρ 提成 p 的问题）。"""
        d = route_pdf(total_pages=50, scanned_pages=0, formula_pages=30,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_MINERU
        assert d.use_mineru

    def test_table_heavy_uses_mineru(self):
        """文字层但表格多 → 走 MinerU。"""
        d = route_pdf(total_pages=48, scanned_pages=0, table_pages=20,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_MINERU

    def test_image_heavy_uses_mineru(self):
        """文字层但图片多 → 走 MinerU（图内文字可被识别）。"""
        d = route_pdf(total_pages=60, scanned_pages=0, image_pages=25,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_MINERU

    def test_simple_text_layer_stays_fast(self):
        """纯文字层 + 无公式/表格/图片 → PyMuPDF 快通道。"""
        d = route_pdf(total_pages=30, scanned_pages=0, formula_pages=0, table_pages=0, image_pages=0,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_TEXT
        assert not d.use_mineru

    def test_few_formula_pages_keeps_text(self):
        """公式页太少（< 阈值）→ 仍走文字层。"""
        d = route_pdf(total_pages=100, scanned_pages=0, formula_pages=3,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_TEXT

    def test_complexity_ratio_threshold(self):
        """复杂页占比低（< complex_page_ratio）→ 文字层。"""
        d = route_pdf(total_pages=200, scanned_pages=0, table_pages=10,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_TEXT

    def test_mixed_scan_and_formula_uses_mineru(self):
        """扫描 + 公式混合 → MinerU。"""
        d = route_pdf(total_pages=40, scanned_pages=30, formula_pages=10,
                      policy=_policy(mineru_enabled=True, scan_engine="mineru"))
        assert d.doc_level == ENGINE_MINERU

    def test_mineru_disabled_keeps_rapid_for_complex(self):
        """MinerU 未启用 → 即使复杂也不走 MinerU。"""
        d = route_pdf(total_pages=50, scanned_pages=0, formula_pages=30,
                      policy=_policy(mineru_enabled=False, scan_engine="rapid"))
        assert d.doc_level == ENGINE_TEXT  # 无扫描页 → 文字层（MinerU 关闭时复杂文字层走 PyMuPDF）
