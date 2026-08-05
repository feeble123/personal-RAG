"""PDF 分层解析（质量优先，核心设计）。

策略：
1. PyMuPDF 逐页提取文本层 + 质量检测（有效字符密度 / 乱码比例）
2. 文字层路径：按版面块重建段落 + 表格检测（find_tables）+ 章节树重建（字号/编号特征）+ 页眉页脚过滤
3. 无文本 / 乱码 → 300dpi 渲染 + OCR 路径（RapidOCR 默认，PaddleOCR 可选）

三种难点 PDF：
- 图片/扫描版：无文本层 → 直接 OCR
- Word 转换版：有文本层但样式拍平 → 内容启发式重建标题；乱码(CID �) → 自动转 OCR
- OCR 版：文本层是历史 OCR 结果 → 错字/乱码率高则重新 OCR
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import fitz  # PyMuPDF

from app.core.config import settings
from app.services.parser.base import DocumentParser, ParsedBlock, ParsedDocument
from app.services.parser.headings import (
    BARE_NUM_RE,
    SIZE_RATIO_THRESHOLD,
    _looks_like_title_by_size,
    detect_heading,
    heading_level,
    line_height_from_box,
    median,
)
from app.services.parser.ocr import OCRResult, ocr_image
from app.services.parser.ocr_progress import clear_progress, set_progress

logger = logging.getLogger(__name__)

# 页眉/页脚过滤：3 页以上重复出现在首/末位置的非正文行
# 「― 44 ―」样式页码行（—–―−- 包围数字）也必须过滤——否则字号兜底会把它当标题
_HEADER_FOOTER_RE = re.compile(
    r"^\s*\d{1,4}\s*$"
    r"|^第\s*\d+\s*页"
    r"|^\d{1,4}/\d{1,4}\s*$"
    r"|[—–―−-]\s*\d{1,4}\s*[—–―−-]$"
)
# 文本层逐行收集时用的页码过滤：**不含纯数字**——
# 纯数字行可能是「编号/标题拆行」PDF 里的标题编号（如「1 / 总则」），
# 必须保留给拆行合并判定；拆行合并失败后再按页码跳过。
_PAGE_MARKER_RE = re.compile(
    r"^第\s*\d+\s*页"
    r"|^\d{1,4}/\d{1,4}\s*$"
    r"|[—–―−-]\s*\d{1,4}\s*[—–―−-]$"
)


def _starts_with_chinese(s: str) -> bool:
    """是否以中文字符开头（拆行合并的标题行必须如此，排除「GB/T …」编号引用行）。"""
    return bool(s) and "一" <= s[0] <= "龥"


def _clean_text(raw: str) -> str:
    """清洗文本：压缩空白、去除孤立空行。"""
    lines = [ln.strip() for ln in raw.splitlines()]
    # 去掉空行与纯页码行
    cleaned = [ln for ln in lines if ln and not _HEADER_FOOTER_RE.match(ln)]
    return "\n".join(cleaned)


def _count_text_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def _garble_ratio(text: str) -> float:
    """乱码占比：替换字符 � 与私用区字符。"""
    if not text:
        return 0.0
    bad = text.count("�")
    bad += sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    total = len(text)
    return bad / total if total else 0.0


def _is_heading(text: str, max_size: float, body_size: float) -> bool:
    """文字层标题判定：统一编号模式（1/2 级）或 字号显著大于正文（含无编号标题）。"""
    stripped = text.strip()
    if not stripped or len(stripped) > 60:
        return False
    if heading_level(stripped):
        return True
    # 字号条件：明显大于正文且像标题（复用 OCR 路径的保守判定：短、含中文、
    # 无公式符号、非标点结尾）。表格页小字号表格会拉低 body_size，正文句子碎片
    # （如「义见表1.2。」）会被误判为标题，因此必须按标题样严格过滤。
    if body_size:
        return _looks_like_title_by_size(stripped, max_size / body_size)
    return False


def _font_stats(page) -> tuple[float, float, float]:
    """返回 (最大字号, 正文常见字号, 最大字号行是否粗体)。"""
    d = page.get_text("dict")
    sizes: dict[float, int] = {}
    max_size = 0.0
    max_bold = False
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            line_max = 0.0
            for span in line.get("spans", []):
                size = span.get("size", 0.0)
                line_max = max(line_max, size)
                sizes[size] = sizes.get(size, 0) + len(span.get("text", ""))
            if line_max > max_size:
                max_size = line_max
                max_bold = bool(span.get("flags", 0) & 16)  # bold flag
    # 正文常见字号 = 字符数加权中位数（取字符数最大的字号）
    body_size = max(sizes, key=sizes.get) if sizes else 12.0
    return max_size, body_size, max_bold


def _extract_table_blocks(page, page_no: int, section: str | None) -> list[ParsedBlock]:
    """表格检测：每个表序列化为管道分隔文本，单独成块（block_type=table）。"""
    blocks: list[ParsedBlock] = []
    try:
        tables = page.find_tables()
    except Exception:
        return blocks
    for table in tables.tables:
        try:
            rows = table.extract()
        except Exception:
            continue
        rows = [["" if c is None else str(c).strip() for c in row] for row in rows]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        lines = [" | ".join(r) for r in rows]
        blocks.append(ParsedBlock(text="\n".join(lines), section=section, page=page_no, block_type="table"))
    return blocks


def _page_needs_ocr(page) -> bool:
    """质量检测：是否应走 OCR（扫描版 / 乱码 / 无文本）。"""
    text = page.get_text("text")
    chars = _count_text_chars(text)
    if chars < settings.pdf_text_threshold_per_page:
        return True
    if _garble_ratio(text) > settings.garble_threshold:
        return True
    return False


class PDFParser(DocumentParser):
    extensions = ("pdf",)

    def parse(self, path: Path, filename: str) -> ParsedDocument:
        """分层解析：并行 OCR（扫描版多页提速）。

        流程：分类页 → 渲染 OCR 页为 PNG（主线程，快）→ 线程池并行识别
              → 按页序组装 blocks（保持章节推进顺序）。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        doc = fitz.open(str(path))
        page_count = doc.page_count
        blocks: list[ParsedBlock] = []
        quality: dict = {
            "parser": "pdf",
            "pages": page_count,
            "ocr_pages": 0,
            "text_pages": 0,
            "tables": 0,
            "garble_ratio": 0.0,
            "total_chars": 0,
        }
        current_section: str | None = None

        # 1) 分类页 + 渲染 OCR 页
        ocr_payloads: dict[int, bytes] = {}
        ocr_results: dict[int, OCRResult | None] = {}
        page_order: list[int] = []
        for page_no, page in enumerate(doc, start=1):
            page_order.append(page_no)
            if _page_needs_ocr(page):
                zoom = settings.ocr_dpi / 72.0
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                ocr_payloads[page_no] = pix.tobytes("png")

        # 2) 并行 OCR（onnxruntime session 并发安全）
        # max_workers=2：平衡速度与内存占用（并发过多在服务进程内容易内存尖峰）
        if ocr_payloads:
            set_progress(path.name, stage="ocr", done=0, total=len(ocr_payloads))
            with ThreadPoolExecutor(max_workers=2) as ex:
                futures = {ex.submit(ocr_image, data): pn for pn, data in ocr_payloads.items()}
                for fut in as_completed(futures):
                    pn = futures[fut]
                    try:
                        ocr_results[pn] = fut.result()
                    except Exception:
                        logger.exception("OCR 失败 page=%s", pn)
                        ocr_results[pn] = None
                    set_progress(path.name, done=len(ocr_results))

        # 3) 按页序组装
        for page_no in page_order:
            page = doc[page_no - 1]
            if page_no in ocr_payloads:
                quality["ocr_pages"] += 1
                result = ocr_results.get(page_no)
                conf = result.mean_confidence if result else 0.0
                quality["ocr_confidence"] = quality.get("ocr_confidence", []) + [round(conf, 3)]
                page_blocks = self._blocks_from_ocr(result, page_no, current_section)
            else:
                quality["text_pages"] += 1
                page_blocks, _ = self._parse_page_text(page, page_no, current_section)

            for b in page_blocks:
                if b.block_type == "table":
                    quality["tables"] += 1
                quality["total_chars"] += _count_text_chars(b.text)
                if b.block_type == "heading":
                    current_section = b.text  # 标题更新章节路径
                blocks.append(b)

        quality["blocks"] = len(blocks)
        confs = quality.get("ocr_confidence") or []
        if confs:
            quality["mean_ocr_confidence"] = round(sum(confs) / len(confs), 3)
        doc.close()
        clear_progress(path.name)  # OCR 进度随解析完成清除（向量化阶段由状态列展示）
        return ParsedDocument(blocks=blocks, page_count=page_count, quality=quality)

    def _blocks_from_ocr(
        self, result: OCRResult | None, page_no: int, section: str | None
    ) -> list[ParsedBlock]:
        """OCR 结果 → 块：行级识别 1/2 级标题（编号 + bbox 行高字号），其余按行序组段。

        bbox 行高比是标题识别的关键信号（覆盖「条文说明」「总则」等无编号放大标题）；
        boxes 与行不对齐时降级为纯编号识别。
        """
        if result is None or not result.text:
            return []
        raw_lines = result.text.splitlines()
        boxes = result.boxes or []
        use_boxes = len(boxes) == len(raw_lines)
        heights = [line_height_from_box(b) for b in boxes] if use_boxes else []
        body_h = median([h for h in heights if h > 0])

        blocks: list[ParsedBlock] = []
        i, n = 0, len(raw_lines)
        while i < n:
            line = raw_lines[i].strip()
            if not line or _HEADER_FOOTER_RE.match(line):
                i += 1
                continue
            ratio = (line_height_from_box(boxes[i]) / body_h) if use_boxes and body_h else None
            if detect_heading(line, ratio):
                blocks.append(ParsedBlock(text=line, page=page_no, block_type="heading"))
                i += 1
                continue
            # 收集连续非标题行 → 段落（空行结束段落）
            para_lines = [line]
            i += 1
            while i < n:
                nl = raw_lines[i].strip()
                if not nl:
                    i += 1
                    break
                ratio2 = (line_height_from_box(boxes[i]) / body_h) if use_boxes and body_h else None
                if detect_heading(nl, ratio2):
                    break
                para_lines.append(nl)
                i += 1
            para = "\n".join(para_lines).strip()
            if para:
                blocks.append(
                    ParsedBlock(text=para, section=section, page=page_no, block_type="paragraph")
                )
        return blocks

    def _parse_page_text(self, page, page_no: int, current_section: str | None):
        """文字层路径：版面块重建 + 表格 + 标题检测。

        返回 (blocks, 可能的新章节路径)。
        """
        blocks: list[ParsedBlock] = []
        max_size, body_size, _ = _font_stats(page)
        d = page.get_text("dict")
        # 表格区域 bbox（避免与段落文本重复）
        table_bboxes: list = []
        try:
            table_bboxes = [tuple(t.bbox) for t in page.find_tables().tables]
        except Exception:
            pass

        def _in_table(x0, y0, x1, y1) -> bool:
            for (tx0, ty0, tx1, ty1) in table_bboxes:
                # 中心点在表格内
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                if tx0 <= cx <= tx1 and ty0 <= cy <= ty1:
                    return True
            return False

        # 1) 收集本页全部文本行（跨 block，保持阅读顺序），跳过表格区域/页眉页脚/空行
        lines: list[tuple[str, float]] = []
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            x0, y0, x1, y1 = block.get("bbox", (0, 0, 0, 0))
            if table_bboxes and _in_table(x0, y0, x1, y1):
                continue  # 表格区域交给表格提取
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not text or _PAGE_MARKER_RE.match(text):
                    continue
                line_max = max((s.get("size", 0) for s in line.get("spans", [])), default=0)
                lines.append((text, line_max))

        # 2) 顺序处理：裸编号行 + 紧随中文短标题行 → 合并识别
        #    处理「1 / 总则」「2.1 / 市级组织指挥机构」拆行且全文同字号的 PDF
        #   （3 级条款 3.2.1 x 合并后 _is_heading 判 False，按正文走）
        i, n = 0, len(lines)
        while i < n:
            text, line_max = lines[i]
            if (
                i + 1 < n
                and BARE_NUM_RE.match(text)
                and _starts_with_chinese(lines[i + 1][0])
                and abs(line_max - lines[i + 1][1]) <= 1.0  # 编号与标题须同字号（页码字号更小，排除）
            ):
                nxt, nxt_max = lines[i + 1]
                merged = f"{text} {nxt}".strip()
                if _is_heading(merged, max(line_max, nxt_max), body_size):
                    blocks.append(ParsedBlock(text=merged, page=page_no, block_type="heading"))
                    i += 2
                    continue
            if _is_heading(text, line_max, body_size):
                blocks.append(ParsedBlock(text=text, page=page_no, block_type="heading"))
            else:
                # 未合并的裸数字行（纯页码等孤立数字）跳过，不污染正文
                if not BARE_NUM_RE.match(text):
                    blocks.append(ParsedBlock(text=text, page=page_no, block_type="paragraph"))
            i += 1

        # 3) 表格块（在文本之后追加）
        blocks.extend(_extract_table_blocks(page, page_no, current_section))
        return blocks, None
