"""PDF 目录（TOC）解析：提取权威大纲，用于正文 1/2 级标题识别失败时补全。

纯函数、无 LLM、无 I/O（调用方传入页文本），可独立单测。

设计：
- `is_toc_page(text)`：目录页判定（「目录/目次/CONTENTS」+ 有效条目数 + 页码单调不减，强防误报）。
- `parse_toc_line(line)`：单条目解析 → (number, title, printed_page, level)。
- `compute_offset(entries, body_blocks)`：目录页码(printed_page) ↔ 正文物理页(physical_page) 偏移，
  用正文已识别的 1/2 级标题与目录条目「编号精确前缀匹配」多数投票。
- `align_pages(toc_info)`：回填每条 `physical_page`。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.core.config import settings

# 目录标题行（页眉）：页码行 / 页眉页脚
_PAGE_LINE_RE = re.compile(r"^[\-\—~]?\s*\d{1,3}\s*[\-\—~]?$|^第\s*\d+\s*页$")
# 数字编号前缀：1 / 1.2 / 3.2.1（最多 4 段），后接点/空格再跟标题
_NUM_PREFIX_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,3})\.?\s+")
# 尾部页码：点线 + 可选括号页码（如 ".......... 5"、"……（5）"）
_DOT_LEADER_RE = re.compile(r"([.·…]{2,})\s*\(?(\d{1,3})\)?\s*$")
# 尾部页码：中文括号（如 "（5）"）
_CN_PAREN_PAGE_RE = re.compile(r"（(\d{1,3})）\s*$")
# 尾部页码：纯尾数（如 "总则 5"）
_PLAIN_PAGE_RE = re.compile(r"(\d{1,3})\s*$")
# 目录标题关键词
_TOC_TITLE_RE = re.compile(r"(目录|目\s*录|目次|CONTENTS)", re.IGNORECASE)
# 标题样校验：排除带句读/过长/目录页自身标题。
# 注意：`、`（中文枚举顿号）**不是**坏标点——规范章节标题常见（"标准值、准永久值系数"），
# 若列入会整条丢弃（实测 3.3「可变作用标准值、准永久值系数」因此从 outline 丢失 → 内容并进前一节）。
_TITLE_BAD_RE = re.compile(r"[。；，！？]")
_TOC_HEAD_RE = re.compile(r"目\s*录|目次|CONTENTS")


@dataclass
class TocEntry:
    """一条目录条目。"""

    number: str  # 规范化编号 "1"/"1.2"/"3.2.1"；无编号 ""（如「总则」/「附录A」，靠标题匹配）
    title: str
    printed_page: int | None  # 目录里印的页码（正文页码）
    level: int  # len(number.split("."))；无编号默认 1
    physical_page: int | None = None  # align_pages 后回填：该节起始的正文物理页


@dataclass
class TocInfo:
    """解析出的目录大纲。"""

    entries: list[TocEntry] = field(default_factory=list)
    toc_pages: list[int] = field(default_factory=list)  # 目录页的物理页号
    offset: int | None = None  # 正文物理页 = 目录页码 + offset；None=未对齐
    source: str = "text"  # "text" / "ocr"

    def by_number(self, number: str) -> TocEntry | None:
        for e in self.entries:
            if e.number == number:
                return e
        return None


def _looks_like_toc_title(title: str) -> bool:
    if not title:
        return False
    if len(title) > 40:
        return False
    if _TITLE_BAD_RE.search(title):
        return False
    if _TOC_HEAD_RE.fullmatch(title):
        return False
    return True


# ---- 多行目录解析（处理拆行编号/独立页码等乱格式）----
# 纯编号行（拆行的编号）："2" / "3.1" / "2."
_NUM_ONLY_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,3})\.?\s*$")
# 纯页码行："5"
_PAGE_ONLY_RE = re.compile(r"^\d{1,3}\s*$")
# 点线+页码行："……………………………………… 5 "
_DOT_PAGE_ONLY_RE = re.compile(r"^[.·…]{2,}\s*(\d{1,3})\s*$")
# 水印/页眉（如「钢管购买热线…」「内部使用」）→ 不判标题
_WATERMARK_RE = re.compile(r"电话|微信|https?://|emlog|@|\.com|内部使用|严禁用于商业|引用于|购买热线")
# 附录标签行（附录A/B/…）——无编号条目的「新条目开始」标记
_APPENDIX_LABEL_RE = re.compile(r"^附录\s*[A-Z]")


def _is_title_line(s: str) -> bool:
    """目录条目标题行：含中文、非纯编号/页码、非水印、非「目次」标题本身。"""
    if not s or len(s) > 60:
        return False
    if _NUM_ONLY_RE.match(s) or _PAGE_ONLY_RE.match(s):
        return False
    if _WATERMARK_RE.search(s):
        return False
    if _TOC_HEAD_RE.fullmatch(s):
        return False
    return re.search(r"[一-鿿]", s) is not None


def _clean_pending_title(title: str) -> str:
    """去掉暂存无编号标题的尾部点线及残留页码标记（如「…… E」「…… 31」），保留纯标题。"""
    title = title.strip()
    m = re.search(r"[.·…]{2,}", title)
    if m:
        title = title[: m.start()].rstrip()
    return title.strip()


def _parse_page_entries(text: str) -> list[TocEntry]:
    """把一页文本解析为目录条目。

    处理真实 PDF 的乱格式（GB 50332 实测）：
    - 编号与标题拆行：「2」单独一行 + 「主要符号...」下一行 → 合并
    - 页码独立一行：「管道结构上的作用……………」下一行是「5」→ 附加
    - 点线与页码独立一行：「永久作用标准值」下一行「……………………… 5 」→ 附加
    - **附录条目**（无编号）：「附录A」+「管侧回填土的综合变形模量」+ 独立页码「21」
      → 暂存多行标题，页码行到达时定稿为条目（此前会被整体丢弃 → 附录从 outline 缺失）。

    裸数字行歧义判定（拆行编号 vs 页码）：**后续标题若自带编号前缀**
    （如「4 基本设计规定」），前面的裸数字就是页码；否则是拆行的编号。
    **有暂存附录标题时**，裸数字几乎必是附录的页码（而非下一个标题的拆行编号）
    ——否则附录E 的页码「31」会被误当「条文说明」的编号，导致顺序错乱、页码丢失。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    entries: list[TocEntry] = []
    pending_number: str | None = None
    pending_titles: list[str] = []  # 无编号多行标题（附录标签+描述）暂存，等页码行定稿
    i, n = 0, len(lines)

    def _finalize_pending(page: int | None) -> None:
        """把暂存的无编号多行标题定稿为条目（页码来自独立行；无页码也保留）。"""
        nonlocal pending_titles
        if not pending_titles:
            return
        title = _clean_pending_title(" ".join(t.strip() for t in pending_titles))
        pending_titles = []
        if _looks_like_toc_title(title):
            entries.append(TocEntry(number="", title=title, printed_page=page, level=1))

    while i < n:
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < n else None
        m_num = _NUM_ONLY_RE.match(line)
        # 1) 拆行编号：裸编号 + 下一行是标题 且 该标题不带自带编号 → 暂存，等标题行合并。
        #    附录条目（附录A/B）无数字编号：裸数字在其前是页码而非编号，不作合并。
        if (
            m_num
            and nxt
            and _is_title_line(nxt)
            and not _NUM_PREFIX_RE.match(nxt)
            and not nxt.startswith(("附录", "附 录", "附则", "附 则"))
            and not pending_titles  # 有暂存附录标题时，裸数字是页码而非拆行编号
        ):
            pending_number = m_num.group(1)
            i += 1
            continue
        # 2) 标题行（可能是 pending_number 的标题，或独立条目；附录无编号标题暂存）
        if _is_title_line(line):
            # 新条目开始（编号标题 / 新附录标签）→ 先定稿上一条暂存的无编号标题（无页码）
            if pending_titles and (_NUM_PREFIX_RE.match(line) or _APPENDIX_LABEL_RE.match(line)):
                _finalize_pending(None)
            full = f"{pending_number} {line}" if pending_number else line
            pending_number = None
            e = parse_toc_line(full)
            if e:
                entries.append(e)
            else:
                # 无编号且无同页条目的标题行（附录标签/描述）→ 暂存，页码独立行定稿
                pending_titles.append(line)
            i += 1
            continue
        # 3) 独立页码行 / 点线+页码行：优先定稿暂存的无编号标题，其次附加到上一条无页码条目
        m_dot = _DOT_PAGE_ONLY_RE.match(line)
        if _PAGE_ONLY_RE.match(line) or m_dot:
            pg = int(line) if _PAGE_ONLY_RE.match(line) else int(m_dot.group(1))
            if pending_titles:
                _finalize_pending(pg)
            elif entries and entries[-1].printed_page is None:
                entries[-1].printed_page = pg
            i += 1
            continue
        # 4) 其他（「目次」标题/水印/噪声）：先定稿暂存的无编号标题（无页码），再跳过
        if pending_titles:
            _finalize_pending(None)
        i += 1
    if pending_titles:
        _finalize_pending(None)
    return entries


def parse_toc_line(line: str) -> TocEntry | None:
    """解析单条目录行 → TocEntry；非目录条目返回 None。

    支持：`1. 总则........... 1`、`1 总则……（5）`、`1.1 适用范围 ... 3`。
    """
    s = line.strip()
    if not s or _PAGE_LINE_RE.match(s):
        return None

    num_match = _NUM_PREFIX_RE.match(s)
    if num_match:
        number = num_match.group(1)
        rest = s[num_match.end():]
    else:
        # 无编号（「总则」「附录A」）：只接受短标题样，目录页自身的标题（目 录）不算
        number = ""
        rest = s
        if not _looks_like_toc_title(rest):
            return None

    page, rest = _split_toc_tail(rest)
    title = rest.strip().strip(" .·…")
    if not _looks_like_toc_title(title):
        return None
    if not number and page is None:
        return None  # 无编号又无页码 → 噪声
    level = len(number.split(".")) if number else 1
    return TocEntry(number=number, title=title, printed_page=page, level=level)


# 中文页码残留（如「·四」「十五」）：多为乱码/残留字符，只清理标题不提取页码
# （提取会破坏页码单调性守卫，实测「·四」让 is_toc_page 拒掉整页）
_CN_NUM_PAGE_RE = re.compile(r"[.·…]{2,}\s*·?[一二三四五六七八九十〇零]{1,3}\s*$")


def _split_toc_tail(rest: str) -> tuple[int | None, str]:
    """从「标题+页码」尾部提取 (page, 剩余标题)。点线/中文括号/纯尾数三种页码。"""
    rest = rest.strip()
    m = _DOT_LEADER_RE.search(rest)
    if m:
        return int(m.group(2)), rest[: m.start()]
    m = _CN_PAREN_PAGE_RE.search(rest)
    if m:
        return int(m.group(1)), rest[: m.start()]
    m = _PLAIN_PAGE_RE.search(rest)
    if m:
        # 纯尾数页码：仅当剩余标题部分像标题才接受（防「第2部分」之类误切）
        head = rest[: m.start()].strip().strip(" .·…")
        if _looks_like_toc_title(head):
            return int(m.group(1)), head
    m = _CN_NUM_PAGE_RE.search(rest)
    if m:
        # 中文页码残留：从标题剔除，页码留 None（不参与偏移/单调性）
        return None, rest[: m.start()]
    return None, rest


def is_toc_page(text: str, min_entries: int | None = None, require_keyword: bool = True) -> bool:
    """目录页判定：含「目录/目次/CONTENTS」+ 有效条目数达标 + 条目页码单调不减。

    require_keyword=False 用于**目录续页**（紧跟前一个目录页，通常无「目录」标题）：
    此时只要求条目数（放宽到 2）+ 页码单调。首目录页必须带关键词，防「目录管理」
    「目录设置」等正文标题误报。
    """
    min_entries = min_entries or settings.toc_min_entries
    if require_keyword:
        normalized = re.sub(r"\s+", "", text)
        if not _TOC_TITLE_RE.search(normalized):
            return False
    entries = _parse_page_entries(text)
    numbered = [e for e in entries if e.number]
    if len(numbered) < min_entries:
        return False
    pages = [e.printed_page for e in numbered if e.printed_page is not None]
    if len(pages) < 2:
        return False
    # 页码单调不减（多数情况下强信号；个别分册重排页码会误拒 → 降级为现状，可接受）
    return all(pages[i] <= pages[i + 1] for i in range(len(pages) - 1))


def _num_key(num: str) -> list[int]:
    """规范化编号 → 可比较的整数列表，如 "3.2" → [3, 2]（列表比较保持文档序）。"""
    return [int(x) for x in num.split(".") if x]


def continues_toc(text: str, prev_last_number: str | None, min_entries: int | None = None) -> bool:
    """目录续页判定：无关键词，但须满足目录页结构 + 首条编号接续上一页末条编号。

    真实多页目录的续页编号连续（如 1.3 → 2，或 5 → 5.1）；而条文说明/前言等
    章节清单页编号从 3/4 重启、接不上目录末条（如 31）→ 拒绝，防止误跳正文页
    （只增不减：续页识别失败只是内容按正文/前置块保留，不再删除）。
    """
    if not prev_last_number:
        return False
    if not is_toc_page(text, min_entries=min_entries or 2, require_keyword=False):
        return False
    first = _parse_page_entries(text)
    if not first or not first[0].number:
        return False
    return _num_key(first[0].number) > _num_key(prev_last_number)


def collect_toc_entries(page_texts: dict[int, str]) -> list[TocEntry]:
    """合并多个目录页的条目（保物理页序）。page_texts: {物理页: 页文本}。"""
    entries: list[TocEntry] = []
    for pno in sorted(page_texts):
        entries.extend(_parse_page_entries(page_texts[pno]))
    return entries


def _heading_number(text: str) -> str | None:
    """取正文标题行的编号（供与目录条目精确前缀匹配）。"""
    m = re.match(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,3})\.?\s", text)
    return m.group(1) if m else None


def compute_offset(
    entries: list[TocEntry],
    body_blocks,
    min_matches: int | None = None,
) -> int | None:
    """目录页码 ↔ 物理页偏移：正文已识别的标题块与目录条目编号精确前缀匹配，多数投票。"""
    min_matches = min_matches or settings.toc_min_offset_matches
    offsets: Counter[int] = Counter()
    matched = 0
    for e in entries:
        if not e.number or e.printed_page is None:
            continue
        for b in body_blocks:
            if b.block_type != "heading" or not b.text or not b.page:
                continue
            bn = _heading_number(b.text)
            if bn == e.number:  # 精确匹配（防 1 误配 1.2/10）
                offsets[b.page - e.printed_page] += 1
                matched += 1
                break
    if matched < min_matches or not offsets:
        return None
    top, cnt = offsets.most_common(1)[0]
    if cnt < min_matches:
        return None
    return top


def align_pages(toc_info: TocInfo, offset: int | None) -> None:
    """回填每条 physical_page = printed_page + offset；offset 为 None 时不动。"""
    toc_info.offset = offset
    if offset is None:
        return
    for e in toc_info.entries:
        if e.printed_page is not None:
            e.physical_page = e.printed_page + offset
