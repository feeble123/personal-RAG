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
from app.services.parser.ocr import OCRResult, ocr_image, ocr_image_tiled, ocr_image_union
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


def _pdf_heading_level(block: ParsedBlock) -> int | None:
    """PDF 标题块的层级（IR 用）：heading 块按编号模式估层级，非标题返回 None。"""
    if block.block_type != "heading":
        return None
    lvl = heading_level(block.text)
    return lvl if lvl >= 1 else None


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


# 高频汉字集（判定文本层是否被 CID 字体 ToUnicode 损坏）。
# 正常中文正文由高频字主导（实测常见 PDF 页占比 0.43~0.95）；
# 乱码页把中文映射成生僻字（如 犮犪狊犜…=custom…），占比 ≈0。
_COMMON_HAN = set(
    "的一是了我不人在他有这上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感见明问力理尔点文几定本公特做外孩相西果走将月十实向声车全信重三机工物气每并别真打太新比才便夫再书部水像眼等体却加电主界门利海受听表德少克代员许先口由死安写性马光白或住难望教命花结乐色更拉东神记处让母父应直字场平报友关放至张认接告入笑内英军候民岁往何度山觉路带万男边风解叫任金快原吃妈变通师立象数四失满战远格士音轻目条呢病始达深完今提求清王化空业思切怎非找片罗钱语元喜曾离飞科言干流欢约各即指合反题必该论交终林请医晚制球决传画保读运及则房早院量苦火布品近坐产答星精视五连司巴奇管类未朋且婚台夜青北队久乎越观落尽形影红爸百令周吧识步希亚术留市半热送兴造谈容极随演收首根整式取照办强石古华另句纪接元伟测速笑组带志呼干友王李张吴刘陈黄杨周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛阎段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)


def _chinese_common_ratio(text: str) -> float:
    """常用汉字占比：判定文本层是否乱码。

    真中文正文由高频字主导；CID 乱码页把中文映射成生僻字，常用字占比 ≈0。
    """
    total = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    if total == 0:
        return 0.0
    return sum(1 for ch in text if ch in _COMMON_HAN) / total


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
    """质量检测：是否应走 OCR（扫描版 / 乱码 / 无文本）。

    乱码检测三层：
    1. 字符数不足 → 扫描/空白页；
    2. 替换符 � 与私用区字符占比（部分 CID 字体直接映射到 PUA）；
    3. 常用汉字占比过低（CID 字体 ToUnicode 损坏时中文被映射成生僻字，
       如 犮犪狊…=custom…，无替换符/私用区字符，只能靠常用字占比识别）。
    """
    text = page.get_text("text")
    chars = _count_text_chars(text)
    if chars < settings.pdf_text_threshold_per_page:
        return True
    if _garble_ratio(text) > settings.garble_threshold:
        return True
    if _chinese_common_ratio(text) < settings.chinese_common_threshold:
        return True
    return False


class PDFParser(DocumentParser):
    extensions = ("pdf",)

    def parse(
        self,
        path: Path,
        filename: str,
        chunk_strategy: str = "old",
        parse_mode: str = "fast",
    ) -> ParsedDocument:
        """分层解析：并行 OCR（扫描版多页提速）。

        chunk_strategy：old=经典（目录页当正文，不做大纲）；new=识别目录页提取权威大纲。
        parse_mode：单元 S 文档级后端选择（fast=快速 pipeline / high=高精度 hybrid-engine），
        仅走 MinerU 路由时生效。
        流程：分类页 → 渲染 OCR 页为 PNG（主线程，快）→ 线程池并行识别
              → 按页序组装 blocks（保持章节推进顺序）。

        P1-2 单元D：文档级路由——扫描占比高且启用 MinerU 时，整文档改走 MinerU
        （bake-off 证明扫描件 MinerU 更快更准）；否则维持 RapidOCR 文字层/OCR 路径。
        """
        detect_toc = chunk_strategy == "new"
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # P1-2 单元D+修正：轻量预检多维度复杂度（扫描/公式/表格/图片）→ 路由决策
        from app.services.parser.router import route_pdf

        _pre_doc = fitz.open(str(path))
        _pre_total = _pre_doc.page_count
        _pre_scan = 0
        _pre_formula = 0
        _pre_table = 0
        _pre_image = 0
        for _p in _pre_doc:
            if _page_needs_ocr(_p):
                _pre_scan += 1
            else:
                _ptxt = _p.get_text("text")
                # 公式信号：含数学符号（≈≥≤∑∫∂√×÷±∞ 等）
                if sum(1 for c in _ptxt if c in "≈≥≤∑∫∂√×÷±∞") >= 3:
                    _pre_formula += 1
                # 图片信号
                if _p.get_images():
                    _pre_image += 1
            # 表格信号（find_tables 较慢，仅对文字层页检测）
            if _p.find_tables().tables:
                _pre_table += 1
        _pre_doc.close()
        route = route_pdf(
            total_pages=_pre_total,
            scanned_pages=_pre_scan,
            formula_pages=_pre_formula,
            table_pages=_pre_table,
            image_pages=_pre_image,
        )
        if route.use_mineru:
            logger.info("PDF 路由→MinerU: %s（%s）", path.name, "; ".join(route.reasons))
            from app.services.parser.mineru import MinerUPDFParser

            return MinerUPDFParser().parse(path, filename, chunk_strategy, parse_mode)

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
                futures = {ex.submit(ocr_image_tiled, data): pn for pn, data in ocr_payloads.items()}
                for fut in as_completed(futures):
                    pn = futures[fut]
                    try:
                        ocr_results[pn] = fut.result()
                    except Exception:
                        logger.exception("OCR 失败 page=%s", pn)
                        ocr_results[pn] = None
                    set_progress(path.name, done=len(ocr_results))

        # 3) 按页序组装；前 N 页检测目录页（目录内容单独成「目录」切片，供大纲补全与检索）
        from app.services.parser.toc import (
            TocInfo,
            _parse_page_entries,
            align_pages,
            collect_toc_entries,
            compute_offset,
            continues_toc,
            is_toc_page,
        )

        toc_pages: list[int] = []
        toc_page_texts: dict[int, str] = {}
        toc_source = "text"
        toc_active = False  # 目录流：首目录页必须带「目录」关键词；续页（无关键词）紧跟其后且编号连续
        last_toc_number: str | None = None  # 目录流上一页末条编号（续页连续性判定）
        for page_no in page_order:
            page = doc[page_no - 1]
            in_ocr = page_no in ocr_payloads
            if in_ocr:
                quality["ocr_pages"] += 1
                result = ocr_results.get(page_no)
                conf = result.mean_confidence if result else 0.0
                quality["ocr_confidence"] = quality.get("ocr_confidence", []) + [round(conf, 3)]
                page_text = result.text if result else ""
            else:
                quality["text_pages"] += 1
                page_text = page.get_text("text")

            # P1-3 修 bug：garble_ratio 真实统计（文字层 + OCR 页都算，取最大值）
            # 之前初始化 0 后从不累加 → needs_review 乱码分支永久失效（审计发现）
            page_garble = _garble_ratio(page_text)
            if page_garble > quality.get("garble_ratio", 0.0):
                quality["garble_ratio"] = page_garble

            looks_toc = False
            if detect_toc and page_no <= settings.toc_search_pages:
                if not toc_active:
                    looks_toc = is_toc_page(page_text)
                else:
                    # 续页：无关键词但需「编号连续性」接上上一目录页末条，防条文说明/前言清单页误判
                    looks_toc = continues_toc(page_text, last_toc_number)
            if looks_toc:
                toc_pages.append(page_no)
                toc_active = True
                if in_ocr:
                    toc_source = "ocr"
                toc_page_texts[page_no] = page_text
                entries_here = _parse_page_entries(page_text)
                if entries_here:
                    last_toc_number = entries_here[-1].number or last_toc_number
                continue  # 目录页不进正文块流（内容单独成「目录」切片，只增不减）
            toc_active = False  # 目录流结束

            if in_ocr:
                page_blocks = self._blocks_from_ocr(result, page_no, current_section)
            else:
                page_blocks, _ = self._parse_page_text(page, page_no, current_section)

            for b in page_blocks:
                if b.block_type == "table":
                    quality["tables"] += 1
                quality["total_chars"] += _count_text_chars(b.text)
                if b.block_type == "heading":
                    current_section = b.text  # 标题更新章节路径
                blocks.append(b)

        quality["blocks"] = len(blocks)

        # 4) 目录权威大纲（物理↔正文页码偏移按正文标题多数投票）
        outline = None
        if toc_pages and toc_page_texts:
            entries = collect_toc_entries(toc_page_texts)
            if entries:
                offset = compute_offset(entries, blocks)
                outline = TocInfo(entries=entries, toc_pages=sorted(toc_pages), offset=offset, source=toc_source)
                align_pages(outline, offset)
                quality.update(
                    {
                        "toc_pages": len(toc_pages),
                        "toc_entries": len(entries),
                        "toc_offset": offset,
                        "outline_source": toc_source,
                    }
                )
        confs = quality.get("ocr_confidence") or []
        if confs:
            quality["mean_ocr_confidence"] = round(sum(confs) / len(confs), 3)

        # P1-3 质量门禁：低质解析标记 needs_review（不自动 active，人工复核）。
        # 触发条件：OCR 页占比高且置信度低 / 乱码率高 / 文本过少。
        ocr_pages = quality.get("ocr_pages", 0)
        total_pages = page_count or 1
        mean_conf = quality.get("mean_ocr_confidence")
        garble = quality.get("garble_ratio", 0.0)
        needs_review = False
        reasons = []
        if ocr_pages / total_pages > 0.5 and mean_conf is not None and mean_conf < 0.5:
            needs_review = True
            reasons.append(f"OCR 占比高且置信度低 (mean={mean_conf:.2f})")
        if garble > settings.garble_threshold * 2:
            needs_review = True
            reasons.append(f"乱码率过高 (garble={garble:.3f})")
        if quality.get("total_chars", 0) < 500:
            needs_review = True
            reasons.append(f"文本量过少 ({quality.get('total_chars', 0)} 字)")
        if needs_review:
            quality["needs_review"] = True
            quality["review_reasons"] = "; ".join(reasons)

        doc.close()
        clear_progress(path.name)  # OCR 进度随解析完成清除（向量化阶段由状态列展示）
        # P1-1：blocks → IR elements（PDF 标题是推断的，标记 inferred_heading）
        elements = [
            b.to_element(i, "pdf", heading_level=_pdf_heading_level(b))
            for i, b in enumerate(blocks)
        ]
        return ParsedDocument(
            blocks=blocks,
            page_count=page_count,
            quality=quality,
            outline=outline,
            toc_texts=toc_page_texts,
            elements=elements,
        )

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

    def repair_ocr_gaps(
        self, path: Path, parsed: ParsedDocument, gaps: list[dict]
    ) -> list[ParsedBlock] | None:
        """断号修复：对断号页用更高条带数重 OCR，补回缺失条款则重建该页 blocks。

        自校验：仅当重 OCR 文本确实出现缺失条款号时才替换该页块；规范本身跳号
        （重 OCR 也补不回）则不改动，免疫误报。返回新 blocks；无改善返回 None。
        """
        from collections import defaultdict

        from app.services.parser.clause_gap import _contains_clause

        affected: dict[int, list[str]] = {}
        for g in gaps:
            for pno in g["pages"]:
                affected.setdefault(pno, []).extend(g["missing_full"])
        if not affected:
            return None
        doc = fitz.open(str(path))
        rebuilt: dict[int, list[ParsedBlock]] = {}
        changed = False
        try:
            for pno, missing_nums in affected.items():
                page = doc[pno - 1]
                zoom = settings.ocr_dpi / 72.0
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                # 三路并集（整页+3条带+6条带）：单种配置漏的行不同，并集互补最大化恢复
                res = ocr_image_union(pix.tobytes("png"))
                orig = [b for b in parsed.blocks if b.page == pno]
                # 该页起始章节上下文 = 页内首个带 section 的块（跨页标题续接）
                section_hint = next((b.section for b in orig if b.section), None)
                recovered = [m for m in set(missing_nums) if _contains_clause(res.text, m)]
                if not recovered:
                    continue  # 补不回 → 规范本身无此条（跳号），跳过
                new_blocks = self._blocks_from_ocr(res, pno, section_hint)
                if not new_blocks:
                    continue
                # 内容量守卫（只增不减）：重建版明显少于原版（OCR 漏行/表格化）时
                # 不整页替换——宁保留原页也不丢已有内容；缺的条款留待下次尝试。
                orig_chars = sum(_count_text_chars(b.text) for b in orig)
                new_chars = sum(_count_text_chars(b.text) for b in new_blocks)
                if orig_chars and new_chars < orig_chars * 0.8:
                    logger.warning(
                        "OCR 断号修复 page=%s 重建内容量下降 %.0f→%.0f 字，跳过整页替换（保留原页）",
                        pno, orig_chars, new_chars,
                    )
                    continue
                rebuilt[pno] = new_blocks
                changed = True
                logger.info("OCR 断号修复 page=%s 补回条款=%s", pno, recovered)
        finally:
            doc.close()
        if not changed:
            return None
        grouped = defaultdict(list)
        for b in parsed.blocks:
            grouped[b.page].append(b)
        merged: list[ParsedBlock] = []
        for pno in sorted(grouped):
            merged.extend(rebuilt.get(pno, grouped[pno]))
        return merged

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
