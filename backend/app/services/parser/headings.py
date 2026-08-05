"""统一标题识别：编号模式 + 字号推断（文本层字号 / OCR bbox 行高）。

设计（策略化，预留升级接口）：
- 当前为启发式：零依赖、离线、CPU 可跑，覆盖 GB/T 1.1 规范体例。
- 未来可替换为版面分析模型（PP-Structure / MinerU / Marker / Docling）的输出：
  只需实现 `detect_heading(text, size_ratio)` 的等价判定（版面模型直接给"标题+层级"），
  调用方（pdf.py / chunker.py）无需改动。
- 用户分块方案（2026-08-04 确认）：**1/2 级标题为硬边界，3/4 级条款视为内容按长度切分**，
  因此 3 级及以上条款（x.y.z …）一律不识别为标题。
"""
from __future__ import annotations

import re

# 1 级：章 / 附录 / 规范固定部分（无编号但常见放大排版）
_LEVEL1_RE = [
    re.compile(r"^第[一二三四五六七八九十百千0-9]+[章节篇讲卷部]"),  # 第三章 / 第3章
    re.compile(r"^附录\s*[A-Z]"),  # 附录E
    re.compile(r"^(条文说明|标准用词说明|标准历次版本编写者信息|编制说明|编写说明)$"),
]
# 3 级及以上条款（x.y.z…）→ 内容，不设标题（用户方案）
_CLAUSE_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){2,}\s*[一-龥A-Za-z]")
# 数字编号标题：x / x.y（1 级 / 2 级）
# 末尾 `\.?`：兼容「2. 系统体系架构」这类编号带点但无下一级数字的一级标题
#（原正则要求点号后必须跟数字，导致 doc4 所有一级标题漏检）
_NUM_RE = re.compile(
    r"^(\d{1,3})(?:\.(\d{1,3}))?(?:\.(\d{1,3}))?(?:\.(\d{1,3}))?\.?\s*[一-龥A-Za-z]"
)
# 文本层「编号/标题拆行」的裸编号行（1 / 2.1 / 3.2 / 2.）——与下一行短标题合并识别
BARE_NUM_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3})*\.?\s*$")
# 中文序号：一、二、…  / （一）（二）…
_CN_NUM_RE = re.compile(r"^[一二三四五六七八九十]+、")
_PAREN_NUM_RE = re.compile(r"^[（(][一二三四五六七八九十]+[）)]\s*")

# 字号（或 OCR 行高）与正文中位数之比阈值：≥1.15 视为标题
SIZE_RATIO_THRESHOLD = 1.15
# 无编号（仅字号识别）标题的最大行长
UNNUMBERED_MAX_LEN = 40

# 标题样判定的标点：标题内不应出现的句中标点 / 标题不应以之结尾的标点
_SENTENCE_PUNC = "，。；：？！…·"
_TITLE_END_PUNC = "，。、；：？！…·,;:.?!“”\"'）】》」』"


def _looks_like_numbered_title(s: str, max_len: int = 30) -> bool:
    """编号标题的「标题样」校验：短、含中文、无句中标点、非标点结尾、无目录点线。

    `_NUM_RE` 只保证「以数字开头」——正文行（如「1 小时内报本级防办，…」
    「147 号）」「100 毫米以上降雨，…」）同样匹配，必须像标题才算数，
    否则章节栈会被内容行污染（实测 doc4/doc6 因此整份文档章节树崩溃）。
    """
    s = s.strip()
    if not s or len(s) > max_len:
        return False
    if not re.search(r"[一-龥]", s):
        return False
    if re.search(rf"[{_SENTENCE_PUNC}]", s):
        return False
    if re.search(r"[（()×√—−+/\\*]", s):  # 括号/公式符号（附录条款「1.4 分类属性（props）…」）
        return False
    if re.search(r"\.{2,}", s):  # 目录点线（「1.2 规范性引用文件……1」）
        return False
    if s[-1] in _TITLE_END_PUNC:
        return False
    return True


def heading_level(text: str) -> int:
    """编号模式 → 标题层级。

    - 1 级：第X章 / 附录X / 条文说明等固定部分
    - 2 级：x.y 标题（如「3.2 引用标准」）、「一、」序号
    - 3/4 级：x.y.z 条款一律返回 0（视为内容，不设标题）
    - 无匹配返回 0
    """
    s = text.strip()
    if not s:
        return 0
    for pat in _LEVEL1_RE:
        if pat.match(s):
            return 1
    if _CLAUSE_RE.match(s):
        return 0
    m = _NUM_RE.match(s)
    if m and _looks_like_numbered_title(s):
        return sum(1 for g in m.groups() if g)
    if _CN_NUM_RE.match(s) or _PAREN_NUM_RE.match(s):
        return 2
    return 0


def _is_clause(text: str) -> bool:
    """x.y.z 条款号（3 级及以上）→ 一律视为内容，不做标题。"""
    return bool(_CLAUSE_RE.match(text.strip()))


def detect_heading(text: str, size_ratio: float | None = None) -> bool:
    """综合判断一行是否为标题。

    size_ratio：本行字号（或 OCR 行高）与正文中位数之比。
    有编号 → 标题；3 级及以上条款 → 内容（即使字号放大）；
    无编号但明显放大（≥SIZE_RATIO_THRESHOLD）且像标题（短、无公式符号、非标点结尾）→ 标题。
    """
    s = text.strip()
    if not s:
        return False
    if heading_level(s):
        return True
    if _is_clause(s):
        return False  # 条款号不算标题，避免条文说明/表格行污染层级
    if _looks_like_title_by_size(s, size_ratio):
        return True
    return False


def _looks_like_title_by_size(s: str, size_ratio: float | None) -> bool:
    """字号/行高判定的保守化：只认「像标题」的短行。

    排除 OCR 内容碎片误判（实测 7.4 公式节被一行「为"(×.×.×-1)""…"等。」
    当标题切开；「措施。」「专家。」等也常被误判）。条件：
    - 字号比 ≥ 阈值
    - 行长 ≤ 20（标题短）
    - 不含公式/编号符号（（）()×√—−+*/. 等）
    - 不以标点结尾（标题一般以中文字符收尾）
    """
    if size_ratio is None or size_ratio < SIZE_RATIO_THRESHOLD:
        return False
    if len(s) > 20:
        return False
    if not re.search(r"[一-龥]", s):
        return False  # 必须含中文（过滤「― 44 ―」等页码行）
    if re.search(r"[（）()×√—−+/\\*]", s):
        return False
    if s[-1] in "。，、；：？！…·,;:.?!“”\"'":
        return False
    return True


def line_height_from_box(box) -> float:
    """OCR 包围盒 → 行高（像素）。box 为四点多边形 [[x, y], ...]。"""
    if not box:
        return 0.0
    ys = [pt[1] for pt in box]
    return max(ys) - min(ys)


def median(values: list[float]) -> float | None:
    """中位数；空列表返回 None。"""
    if not values:
        return None
    sv = sorted(values)
    n = len(sv)
    return sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
