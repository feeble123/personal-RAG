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


def ocr_image(image_bytes: bytes) -> OCRResult:
    """对单张页面图片执行 OCR。保留每行包围盒（标题识别的字号推断信号）。"""
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
    text = "\n".join(lines)
    mean_conf = sum(confs) / len(confs) if confs else 0.0
    return OCRResult(text=text, lines=lines, boxes=boxes, mean_confidence=mean_conf)
