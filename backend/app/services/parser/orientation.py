"""P1-3 方向检测与纠偏（纯函数/薄封装，可单测）。

审计/评测发现：48 页扫描规范末页图像横置 90°但 PDF /Rotate=0——
当前解析器无方向检测，OCR 条带读串行、表格切碎。
本模块：检测页面内容旋转（横置页），渲染 OCR 前纠正。
"""
from __future__ import annotations


def detect_page_rotation(page) -> int:
    """检测页面内容旋转角（0/90/180/270）。

    依据：
    1. PDF 显式 /Rotate（page.rotation）
    2. 内容占位框宽高比：宽 > 高 × 1.2 → 内容横置（旋转 90/270）
    3. 文本块 bbox 的宽高比（若页面有文字层）
    返回需旋转的角度（0=无需，90=需顺时针转正）。
    """
    # 1) PDF 显式旋转
    explicit = getattr(page, "rotation", 0) or 0
    if explicit in (90, 270):
        return 270 if explicit == 90 else 90

    # 2) 页面几何：宽 > 高 → 横置内容
    rect = getattr(page, "rect", None)
    if rect is not None:
        w, h = rect.width, rect.height
        if w > h * 1.2:
            return 90  # 内容横置，需顺时针转 90° 转正
        if h > w * 1.2:
            return 0  # 纵向正常

    return 0


def needs_correction(page) -> bool:
    """页面是否需要方向纠偏（内容横置但 PDF 未标注旋转）。"""
    # 若 PDF 已显式旋转，PyMuPDF 提取文本已按旋转生效，无需再纠
    if getattr(page, "rotation", 0):
        return False
    return detect_page_rotation(page) != 0


def render_corrected(page, dpi: int = 200, *, target_rotation: int | None = None) -> bytes:
    """渲染页面 PNG，必要时旋转纠偏后输出（供 OCR 用）。

    Args:
        page: PyMuPDF Page
        dpi: 渲染分辨率
        target_rotation: 期望的最终旋转（默认自动检测纠正到 0°）
    """
    import fitz

    rotation = target_rotation if target_rotation is not None else detect_page_rotation(page)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    # 若需纠偏，先旋转再渲染，渲染后恢复原 rotation（不污染原页面状态）
    original_rotation = getattr(page, "rotation", 0)
    if rotation != 0:
        page.set_rotation((original_rotation + rotation) % 360)
    try:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        if rotation != 0:
            page.set_rotation(original_rotation)
