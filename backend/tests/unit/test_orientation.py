"""P1-3 单元E：方向检测测试（横置页纠偏）。"""
from __future__ import annotations

import fitz

from app.services.parser.orientation import (
    detect_page_rotation,
    needs_correction,
    render_corrected,
)


class _FakeRect:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class _FakePage:
    def __init__(self, w, h, rotation=0):
        self.rect = _FakeRect(w, h)
        self.rotation = rotation

    def get_pixmap(self, **kw):
        return _FakePix()

    def set_rotation(self, r):
        self.rotation = r


class _FakePix:
    def tobytes(self, *a, **k):
        return b"PNG"


class TestDetectRotation:
    def test_portrait_no_correction(self):
        """纵向页面（高 > 宽）→ 0（无需纠偏）。"""
        page = _FakePage(600, 800)
        assert detect_page_rotation(page) == 0

    def test_horizontal_detected(self):
        """横置内容（宽 > 高）→ 需转 90°。"""
        page = _FakePage(800, 600)
        assert detect_page_rotation(page) == 90

    def test_explicit_rotation_handled(self):
        """PDF 显式 /Rotate=90 → 转回 270 纠偏。"""
        page = _FakePage(600, 800, rotation=90)
        assert detect_page_rotation(page) == 270


class TestNeedsCorrection:
    def test_no_correction_portrait(self):
        """纵向 + 无旋转 → 不纠偏。"""
        assert needs_correction(_FakePage(600, 800)) is False

    def test_correct_horizontal(self):
        """横置 + 无显式旋转 → 需要纠偏。"""
        assert needs_correction(_FakePage(800, 600)) is True

    def test_explicit_rotation_skips(self):
        """PDF 已显式旋转 → 不再纠偏（PyMuPDF 已按旋转生效）。"""
        assert needs_correction(_FakePage(800, 600, rotation=90)) is False


class TestRenderCorrected:
    def test_render_corrected_rotates(self):
        """横置页渲染 → 返回 PNG 且旋转被纠正。"""
        page = _FakePage(800, 600)
        data = render_corrected(page, dpi=200)
        assert data == b"PNG"
        # 纠偏后 rotation 恢复（不影响原页）
        assert page.rotation == 0

    def test_render_portrait_no_change(self):
        """纵向页渲染 → 无需旋转。"""
        page = _FakePage(600, 800)
        data = render_corrected(page)
        assert data == b"PNG"
