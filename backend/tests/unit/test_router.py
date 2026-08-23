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
    base = RouterPolicy(mineru_enabled=False, scan_engine="rapid", mineru_min_scan_ratio=0.5, min_scan_pages=2)
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
