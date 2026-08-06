"""OCR 引擎封装：默认 RapidOCR（onnxruntime，纯 pip 轻量、中文效果好）。

升级路径：`OCR_ENGINE=paddle` 时切换 PaddleOCR PP-Structure（版面+表格+公式，质量最高，依赖重）；
也可扩展第三方 OCR API。懒加载——只有扫描版 PDF 需要 OCR 时才导入重依赖。
"""
from __future__ import annotations

import logging
import threading

from app.core.config import settings

logger = logging.getLogger(__name__)


class OCRResult:
    """单页 OCR 结果。

    boxes 与 lines 逐行对齐（四点多边形 [[x, y], ...]），用于标题识别的字号推断。
    """

    __slots__ = ("text", "lines", "boxes", "mean_confidence")

    def __init__(self, text: str, lines: list[str], boxes: list, mean_confidence: float):
        self.text = text
        self.lines = lines
        self.boxes = boxes
        self.mean_confidence = mean_confidence


_engine = None
_engine_name: str | None = None
_init_lock = threading.Lock()


def _get_engine():
    """懒加载 OCR 引擎单例（线程安全：并行 OCR 时并发调用）。"""
    global _engine, _engine_name
    if _engine is not None and _engine_name == settings.ocr_engine:
        return _engine
    with _init_lock:
        if _engine is not None and _engine_name == settings.ocr_engine:
            return _engine
        if settings.ocr_engine == "paddle":
            from paddleocr import PaddleOCR  # type: ignore[import-not-found]

            _engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        else:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

            # 限制单会话线程数：默认全核推理在并发时会互相争抢，设低后线程池并行反而更快
            _engine = RapidOCR(intra_op_num_threads=settings.ocr_intra_op_threads)
        _engine_name = settings.ocr_engine
        logger.info(
            "OCR 引擎加载完成: %s (intra_op_threads=%s)",
            settings.ocr_engine,
            settings.ocr_intra_op_threads,
        )
        return _engine


def _run_engine(image_bytes: bytes) -> tuple[list[str], list, list[float]]:
    """调用 OCR 引擎，返回 (lines, boxes, confs)。"""
    engine = _get_engine()
    ocr_lines: list[tuple[str, list, float]] = []
    if settings.ocr_engine == "paddle":
        result = engine.ocr(image_bytes, cls=True)
        for page in result or []:
            for line in page or []:
                # line = [box, (text, confidence)]
                box = line[0]
                text, conf = line[1][0], line[1][1]
                ocr_lines.append((str(text), box, float(conf)))
    else:
        result, _elapse = engine(image_bytes)
        for item in result or []:
            # item = [box, text, confidence]
            ocr_lines.append((str(item[1]), item[0], float(item[2])))

    lines = [t for t, _, _ in ocr_lines]
    boxes = [b for _, b, _ in ocr_lines]
    confs = [c for _, _, c in ocr_lines]
    return lines, boxes, confs


def ocr_image(image_bytes: bytes) -> OCRResult:
    """对单张页面图片执行 OCR。保留每行包围盒（标题识别的字号推断信号）。"""
    lines, boxes, confs = _run_engine(image_bytes)
    mean_conf = sum(confs) / len(confs) if confs else 0.0
    return OCRResult(text="\n".join(lines), lines=lines, boxes=boxes, mean_confidence=mean_conf)


def ocr_image_union(image_bytes: bytes, tiles: int | None = None) -> OCRResult:
    """多配置合并 OCR：整页 + N 条带 + 2N 条带分别识别后取并集合并。

    单种配置（含分条带）在密集排版下仍可能漏个别行，且不同配置漏的行不同
    （实测整页漏 8.2.3、3 条带漏 8.2.2、6 条带全中）。多配置并集互补，最大化不漏行。
    """
    n = tiles or settings.ocr_tiles
    entries: list[tuple[str, list, float]] = []
    for res in (
        ocr_image(image_bytes),
        ocr_image_tiled(image_bytes, tiles=n),
        ocr_image_tiled(image_bytes, tiles=n * 2),
    ):
        for t, b in zip(res.lines, res.boxes):
            entries.append((str(t), b, 1.0))
    entries.sort(key=lambda e: (_box_center_y(e[1]), _box_center_x(e[1])))
    return _merge_tiled_lines(entries)


def _box_center_y(box) -> float:
    return sum(p[1] for p in box) / len(box)


def _box_center_x(box) -> float:
    return sum(p[0] for p in box) / len(box)


def _merge_tiled_lines(entries: list[tuple[str, list, float]]) -> OCRResult:
    """合并分条带 OCR 行：重叠条带会把同一行识别两次，按 y 邻近 + 文本包含去重，保留更完整文本。

    entries: (text, 全局坐标 box, conf)，已按阅读序（y 中心 → x 中心）排列。
    """
    heights = [max(p[1] for p in box) - min(p[1] for p in box) for _, box, _ in entries]
    med_h = sorted(heights)[len(heights) // 2] if heights else 30.0
    merged: list[tuple[str, list, float]] = []
    for text, box, conf in entries:
        yc = _box_center_y(box)
        if merged:
            prev_text, prev_box, _ = merged[-1]
            prev_yc = _box_center_y(prev_box)
            # 同一行（重叠条带重复识别）：y 邻近且文本相同/包含 → 保留更长文本
            if abs(yc - prev_yc) < med_h * 0.6 and (text == prev_text or text in prev_text or prev_text in text):
                if len(text) > len(prev_text):
                    merged[-1] = (text, box, conf)
                continue
        merged.append((text, box, conf))
    lines = [t for t, _, _ in merged]
    boxes = [b for _, b, _ in merged]
    confs = [c for _, _, c in merged]
    mean_conf = sum(confs) / len(confs) if confs else 0.0
    return OCRResult(text="\n".join(lines), lines=lines, boxes=boxes, mean_confidence=mean_conf)


def ocr_image_tiled(image_bytes: bytes, tiles: int | None = None) -> OCRResult:
    """分条带 OCR：整页检测模型在密集排版下偶发漏行（实测漏读条款行如 8.2.3），
    拆成横向重叠条带分别识别后合并，保证不漏行。

    tiles：条带数（默认 settings.ocr_tiles）；每个条带与相邻条带重叠 20% 高度，
    确保跨条带的行完整落在至少一个条带内（重叠处重复行按文本包含去重）。
    """
    import io

    from PIL import Image

    n = tiles or settings.ocr_tiles
    if n <= 1:
        return ocr_image(image_bytes)
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    strip_h = h / n
    overlap = max(30, int(strip_h * 0.2))
    entries: list[tuple[str, list, float]] = []
    for i in range(n):
        top = int(max(0, i * strip_h - (overlap if i > 0 else 0)))
        bottom = int(min(h, (i + 1) * strip_h + overlap))
        crop = img.crop((0, top, w, bottom))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        lines, boxes, confs = _run_engine(buf.getvalue())
        for text, box, conf in zip(lines, boxes, confs):
            box_global = [[x, y + top] for (x, y) in box]
            entries.append((str(text), box_global, float(conf)))
    entries.sort(key=lambda e: (_box_center_y(e[1]), _box_center_x(e[1])))
    return _merge_tiled_lines(entries)
