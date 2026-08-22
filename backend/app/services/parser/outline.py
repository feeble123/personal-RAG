"""大纲注入：用 TOC 权威大纲补全正文缺失的章节边界。

- 硬注入（1/2 级）：TOC 为权威。正文已识别的同编号标题跳过；否则优先
  「正文段落行首定位拆分升格」，其次「TOC 页码对齐物理页插入虚拟标题」，
  兜底「插到下一个兄弟编号之前」（顺序锚定）。
- 软注入（3/4/5 级，LLM 确认缺失）：只做软边界（chunker 长度切分时优先断），
  不升级为硬边界。正文找不到编号且无 TOC 页码时插「仅编号」边界。

所有注入 heading 一律 `section=None`——避免 chunker 的 `has_heading_section`
预扫描误判进「解析器路径模式」导致编号栈失效。
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from app.services.parser.base import ParsedBlock
from app.services.parser.headings import _looks_like_numbered_title
from app.services.parser.toc import TocInfo, _heading_number

logger = logging.getLogger(__name__)


@dataclass
class Injection:
    """一条注入操作：在 blocks[block_index] 之前插入标题块；split 时拆该段落。"""

    text: str
    page: int | None = None
    block_index: int | None = None  # None = 无法定位，跳过
    block_type: str = "heading"  # "heading" 硬边界 / "soft_heading" 软边界
    split_line_index: int | None = None  # 拆 blocks[block_index] 在第 split_line_index 行（0 起）
    level: int = 1
    number: str = ""

    @property
    def priority(self) -> int:
        return 0 if self.block_type == "heading" else 1


def _num_key(num: str) -> list[int]:
    # 空段（""、无编号条目）→ []；"31." → [31]。供排序键与兄弟比较，绝不崩。
    return [int(x) for x in num.split(".") if x]


def _title_split_hint(b: ParsedBlock, number: str, title: str) -> int | None:
    """锚点块首行就是无编号标题（strip 点线后 == 目录标题）→ 拆该行升格，避免标题重复。"""
    if not title or len(title) < 2:
        return None
    first_line = (b.text or "").split("\n")[0].strip().strip(" .·…")
    if first_line == title:
        return 0
    return None


def _first_block_at_or_after(
    blocks: list[ParsedBlock], page: int, title: str | None = None
) -> int | None:
    """第一个 `page >= page` 的正文块下标；全无页码或越界返回 None。

    title 给出时优先「目标页(±1)内块首行包含目录标题且行短（像标题）」的块——
    页码锚定只定位到页，页内可能还有前一节的尾巴（如 4.1 的公式符号/4.1.5-7 在
    4.2 所在页），页内标题匹配把边界插到标题行前，避免把前一节内容吞进本节。
    """
    first_at: int | None = None
    for i, b in enumerate(blocks):
        if b.page is None:
            continue
        if b.page < page:
            continue
        if first_at is None:
            first_at = i
        if b.page > page + 1:
            break
        if title and len(title) >= 2:
            first_line = (b.text or "").split("\n")[0].strip()
            if title in first_line and 0 < len(first_line) <= 40:
                return i
    return first_at


def _next_sibling_found(number: str, found) -> object | None:
    """同一父节下、数值大于 number 的最小已找到编号（供顺序锚定）。"""
    parts = number.split(".")
    parent = ".".join(parts[:-1])
    level = len(parts)
    key = _num_key(number)
    nxt = None
    for f in found:
        fparts = f.number.split(".")
        if len(fparts) != level or not f.number.startswith(parent + "."):
            continue
        if _num_key(f.number) > key and (nxt is None or _num_key(f.number) < _num_key(nxt.number)):
            nxt = f
    return nxt


def build_hard_injections(
    toc_info: TocInfo, blocks: list[ParsedBlock], found, body_start: int | None = None
) -> list[Injection]:
    """权威大纲硬注入：正文缺失的 TOC 条目注入为硬标题边界。

    深度跟随目录层级——目录有几级大纲就切到几级（一级/二级/三级…都作为硬边界），
    目录未覆盖的更深层级由 LLM 断号软注入兜底。

    定位优先级（在干净块 + 重算偏移后）：
      1. 目录页码对齐物理页（最准，如 doc12 offset=+7）；
      2. 首个一级条目无页码 → 用正文起点页；
      3. 正文行首出现该编号且行像标题 → 原地拆段落升格；
      4. 顺序锚定到下一个兄弟编号之前。
    """
    body_nums = {_heading_number(b.text) for b in blocks if b.block_type == "heading" and b.text}
    body_nums.discard(None)
    found_by_num = {f.number: f for f in found}
    first_level1 = next((e for e in toc_info.entries if e.level == 1 and e.number), None)
    injections: list[Injection] = []
    for e in toc_info.entries:
        if not e.number:
            # 无编号条目（条文说明/附录A…）：正文已有标题匹配 heading（含附录标签前缀）→ 跳过；
            # 否则按物理页锚点注入标题边界（正文标题缺失/乱码时的兜底，只增不减）
            if any(
                _heading_matches_entry(b.text or "", e)
                for b in blocks
                if b.block_type == "heading"
            ):
                continue
            if e.physical_page is not None:
                idx = _first_block_at_or_after(blocks, e.physical_page, title=e.title)
                if idx is not None:
                    injections.append(
                        Injection(
                            text=e.title, page=e.physical_page,
                            block_index=idx, split_line_index=_title_split_hint(blocks[idx], "", e.title),
                            block_type="heading",
                            level=e.level, number="",
                        )
                    )
            continue
        if e.number in body_nums:
            continue  # 正文已识别，无需注入
        # 1) 页码锚定（干净块重算偏移后最准）；子级标题优先跟在父级正文标题之后，
        #    防同页多个标题时子级插到父级前面（如 3.2 插到 3 之前导致「2 主要符号 / 3.2」）
        if e.physical_page is not None:
            idx = _first_block_at_or_after(blocks, e.physical_page, title=e.title)
            # 子级标题：仅当页码锚点落在父级之前/同一位置时，改插到父级之后
            # （防同页多个标题时子级插到父级前面，如 3.2 插到 3 之前导致「2 主要符号 / 3.2」）
            if e.level >= 2:
                parent_num = ".".join(e.number.split(".")[:-1])
                parent_index = next(
                    (bi for bi, b in enumerate(blocks)
                     if b.block_type == "heading" and _heading_number(b.text) == parent_num),
                    None,
                )
                if parent_index is not None and (idx is None or idx <= parent_index):
                    idx = parent_index + 1
            if idx is not None:
                # 锚点块首行就是无编号标题 → 拆行升格（避免「4.2 承载…/承载…」标题重复）
                injections.append(
                    Injection(
                        text=f"{e.number} {e.title}".strip(), page=e.physical_page,
                        block_index=idx, split_line_index=_title_split_hint(blocks[idx], e.number, e.title),
                        block_type="heading", level=e.level, number=e.number,
                    )
                )
                continue
        # 2) 首个一级条目无页码 → 用正文起点页
        if first_level1 is not None and e.number == first_level1.number and body_start:
            idx = _first_block_at_or_after(blocks, body_start)
            if idx is not None:
                injections.append(
                    Injection(
                        text=f"{e.number} {e.title}".strip(), page=body_start,
                        block_index=idx, block_type="heading", level=e.level, number=e.number,
                    )
                )
                continue
        # 3) 行首拆分（正文确有该编号且行像标题；条款/公式行不像标题 → 不误切）
        f = found_by_num.get(e.number)
        if f and f.line_index is not None and _looks_like_numbered_title(f.text):
            injections.append(
                Injection(
                    text=f.text, page=f.page, block_index=f.block_index,
                    split_line_index=f.line_index, block_type="heading",
                    level=e.level, number=e.number,
                )
            )
            continue
        # 4) 顺序锚定到下一个兄弟编号之前
        nxt = _next_sibling_found(e.number, found)
        if nxt is not None:
            injections.append(
                Injection(
                    text=f"{e.number} {e.title}".strip(), page=nxt.page,
                    block_index=nxt.block_index, block_type="heading",
                    level=e.level, number=e.number,
                )
            )
    return injections


def build_soft_injections(
    toc_info: TocInfo, confirmed: set[str], found, blocks: list[ParsedBlock] | None = None
) -> list[Injection]:
    """3/4/5 级软边界：LLM 确认缺失的编号，注入为软标题（不升级硬边界）。

    锚点优先级：正文行首定位拆分 > TOC 页码对齐物理页 > 仅编号边界（下一个兄弟之前）。
    """
    blocks = blocks or []
    found_by_num = {f.number: f for f in found}
    injections: list[Injection] = []
    for num in sorted(confirmed, key=_num_key):
        f = found_by_num.get(num)
        if f and f.line_index is not None:
            injections.append(
                Injection(
                    text=f.text, page=f.page, block_index=f.block_index,
                    split_line_index=f.line_index, block_type="soft_heading",
                    level=len(num.split(".")), number=num,
                )
            )
            continue
        e = toc_info.by_number(num) if toc_info else None
        if e and e.physical_page is not None:
            idx = _first_block_at_or_after(blocks, e.physical_page, title=e.title)
            if idx is not None:
                injections.append(
                    Injection(
                        text=f"{e.number} {e.title}".strip(), page=e.physical_page,
                        block_index=idx, block_type="soft_heading",
                        level=len(num.split(".")), number=num,
                    )
                )
                continue
        # 仅编号边界：插到下一个兄弟编号之前（保序）
        nxt = _next_sibling_found(num, found)
        if nxt is not None:
            injections.append(
                Injection(
                    text=num, page=nxt.page, block_index=nxt.block_index,
                    block_type="soft_heading", level=len(num.split(".")), number=num,
                )
            )
    return injections


def apply_injections(blocks: list[ParsedBlock], injections: list[Injection]) -> list[ParsedBlock]:
    """把注入应用到块流：拆分段落 / 在目标块前插虚拟标题，返回新块流。

    同一块可挂多条注入：页锚（非 split）按序插在块前；多条 split 按行号排序、
    用游标逐行消费原始行——原始行只读一次，杜绝重复/错位（内容只增不减）。
    """
    if not injections:
        return blocks
    by_idx: dict[int, list[Injection]] = defaultdict(list)
    for inj in injections:
        if inj.block_index is None:
            logger.debug("注入无法定位，跳过: %s", inj.number or inj.text[:20])
            continue
        by_idx[inj.block_index].append(inj)

    out: list[ParsedBlock] = []
    for i, b in enumerate(blocks):
        injs = sorted(by_idx.get(i, []), key=lambda x: (x.priority, _num_key(x.number)))
        nonsplits = [x for x in injs if x.split_line_index is None]
        splits = [x for x in injs if x.split_line_index is not None]
        for inj in nonsplits:
            out.append(
                ParsedBlock(
                    text=inj.text, section=None, page=inj.page or b.page, block_type=inj.block_type
                )
            )
        if splits:
            splits.sort(key=lambda x: x.split_line_index)
            lines = b.text.split("\n")
            cursor = 0
            for inj in splits:
                li = inj.split_line_index
                seg = lines[cursor:li]
                if seg and any(l.strip() for l in seg):
                    out.append(
                        ParsedBlock(
                            text="\n".join(seg).strip(), section=b.section,
                            page=b.page, block_type="paragraph",
                        )
                    )
                out.append(
                    ParsedBlock(text=inj.text, section=None, page=b.page, block_type=inj.block_type)
                )
                cursor = li + 1
            rest = lines[cursor:]
            if rest and any(l.strip() for l in rest):
                out.append(
                    ParsedBlock(
                        text="\n".join(rest).strip(), section=b.section,
                        page=b.page, block_type=b.block_type,
                    )
                )
        else:
            out.append(b)
    return out


_APPENDIX_RE = re.compile(r"^附录\s*([A-Z])")


def _appendix_label(title: str) -> str | None:
    """取目录条目标题的「附录X」前缀（附录A/B/…），无则 None。"""
    m = _APPENDIX_RE.match((title or "").strip())
    return f"附录{m.group(1)}" if m else None


def _heading_matches_entry(text: str, e) -> bool:
    """正文标题是否匹配目录条目：目录标题完整包含 / 附录标签前缀（正文「附录A」↔ 目录「附录A 管侧…」）。"""
    if e.title and len(e.title) >= 2 and e.title in text:
        return True
    lab = _appendix_label(e.title)
    if lab and text.strip().startswith(lab):
        return True
    return False


def _strip_number_prefix(text: str) -> str:
    """剥掉行首编号前缀，返回标题部分（供标题一致性校验）。"""
    m = re.match(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,3})\.?\s*", text)
    return text[m.end():].strip() if m else text.strip()


def _toc_confirmed_heading(toc_info: TocInfo, text: str) -> bool:
    """正文标题是否被目录确认。

    - **有编号**：编号一致 **AND** 标题互含（双向、至少一边 ≥4 字）才确认。
      只按编号匹配会把正文列表项当 1/2 级标题——实测 doc12 里
      「2 正常使用极限状态:…」「1 对粘性土可取」「2 可变作用应包括…」的编号
      撞上「2 主要符号」「1 总则」「3 管道结构上的作用」→ 被误认一级标题、
      压进章节栈、正文句子变成切片 section（用户原则：section 只能来自目录 1/2 级大纲）。
    - **无编号**：按「目录标题完整包含」/附录标签前缀确认（总则/主要符号/附录A…），
      保留其章节身份（只增不减——结构身份也是内容）。
    """
    bn = _heading_number(text)
    body_title = _strip_number_prefix(text)
    for e in toc_info.entries:
        if not e.title or len(e.title) < 2:
            continue
        if bn:
            if e.number == bn and body_title and (
                body_title == e.title  # 精确相等（短标题也认，如「1 总则」）
                or (len(e.title) >= 4 and e.title in body_title)
                or (len(body_title) >= 4 and body_title in e.title)
            ):
                return True
        else:
            if _heading_matches_entry(text, e):
                return True
    return False


_FORMULA_INTRO = {"式中", "其中", "式中:", "式中：", "其中:", "其中：", "注:", "注："}
_FORMULA_SYMBOL_RE = re.compile(
    r"[A-Za-z]|[σγδλαβεθπφψωΩΦΛ]|一一|−−|—|[≤≥≈×÷±]|（\s*(mm|N/|MPa|kN)"
)


def _is_formula_fragment(text: str) -> bool:
    """公式符号行（「式中」「Gik一一第」「t 一一设计壁厚」）——不是结构标题，降级为普通内容。

    判定：是「式中/其中/注：」等公式引导词，或短行（≤30字）含拉丁/Greek/一一/单位等
    公式符号。真章节标题（总则/条文说明/编号条款）不含这些，仍作软边界。
    """
    s = text.strip()
    if s in _FORMULA_INTRO:
        return True
    if len(s) <= 30 and _FORMULA_SYMBOL_RE.search(s):
        return True
    return False


def demote_non_toc_headings(blocks: list[ParsedBlock], toc_info: TocInfo) -> list[ParsedBlock]:
    """把未被目录确认的正文标题降级（用户原则：**只有目录 1/2 级大纲单独切片，其余进内容**）。

    - **有编号但未确认**（正文列表项「2 正常使用极限状态:…」「1 对粘性土可取」、
      非目录条款行）→ **paragraph**（纯内容，连断点都不是，完整并入所在节切片）。
    - **公式碎片** → **paragraph**（并入公式块，防碎片孤岛）。
    - **无编号未确认**（条文说明内部小节「承载能力极限状态计算」等）→ **soft_heading**
      （内容内软断点：不强制起块，缓冲超长时优先在此断，粒度靠长度切分）。

    只降级不进 1/2 级章节栈（目录层级是切片权威），正文句子绝不允许成为 section 名。
    """
    out: list[ParsedBlock] = []
    for b in blocks:
        if b.block_type == "heading" and not _toc_confirmed_heading(toc_info, b.text):
            if _is_formula_fragment(b.text) or _heading_number(b.text):
                # 公式碎片 / 有编号未确认（正文列表项）→ 纯内容段落
                btype = "paragraph"
            else:
                # 无编号未确认（条文说明内部小节）→ 软断点（不强制起块）
                btype = "soft_heading"
            out.append(ParsedBlock(text=b.text, section=None, page=b.page, block_type=btype))
        else:
            out.append(b)
    return out


def find_body_start(toc_info: TocInfo, blocks: list[ParsedBlock]) -> int | None:
    """正文起始物理页：首个一级目录条目的标题在正文标题中最早出现的页。

    用于把正文前的封面/公告/前言/目次等无大纲内容与正文分开（避免污染章节栈）。
    按「编号一致 + 标题互含」双匹配，防封面碎片（如「2. 中国工程建设标准化协会标准」）
    被误当正文开头（目录里 2 的标题是「主要符号」，与封面标题不互含）。
    """
    best: int | None = None
    for e in toc_info.entries:
        if e.level != 1 or not e.title:
            continue
        for b in blocks:
            if b.block_type != "heading" or not b.text or not b.page:
                continue
            bn = _heading_number(b.text)
            if e.number and bn and bn != e.number:
                continue
            # 严格标题匹配：目录标题须完整出现在正文标题中（≥2 字）。
            # 不用「正文标题 ∈ 目录标题」的单向子串——封面碎片单字（如「设」）
            # 会误中「基本设计规定」，把正文起点算到封面页。
            if len(e.title) < 2 or e.title not in b.text:
                continue
            if best is None or b.page < best:
                best = b.page
            break  # 该目录条目只取最早匹配页
    return best


def demote_front_matter(blocks: list[ParsedBlock], body_start: int) -> list[ParsedBlock]:
    """正文前的封面/公告/前言等：标题块降级为普通段落，不参与章节栈。

    这些内容没有多级大纲，保留为无层级前缀的普通 chunk（仍可检索），
    但不再像「2. 中国工程建设标准化协会标准 / … / 目次」那样污染章节前缀。
    """
    out: list[ParsedBlock] = []
    for b in blocks:
        if b.page is not None and b.page < body_start and b.block_type == "heading":
            out.append(ParsedBlock(text=b.text, section=None, page=b.page, block_type="paragraph"))
        else:
            out.append(b)
    return out


def inject_blocks(
    blocks: list[ParsedBlock],
    toc_info: TocInfo | None,
    confirmed: set[str],
    found,
) -> list[ParsedBlock]:
    """完整注入：硬注入（TOC 全层级权威）+ 软注入（LLM 确认的目录外缺失）。

    - 有目录：①按正文起点把封面/公告/前言标题降级为普通段落；
      ②只保留目录确认的正文标题（防章节条款/乱码/公式行污染章节栈）；
      ③在干净块上重算目录页码↔物理页偏移并注入 TOC 全部层级（目录几级切几级）；
      LLM 只补目录之外的更深缺口。
    - 无目录（扫描件目录识别失败）：跳过硬注入，仍做软注入（3/4/5 级靠兄弟连续性）。
    """
    hard: list[Injection] = []
    soft_confirmed = confirmed
    if toc_info:
        from app.services.parser.toc import align_pages, compute_offset

        body_start = find_body_start(toc_info, blocks)
        if body_start:
            blocks = demote_front_matter(blocks, body_start)
        blocks = demote_non_toc_headings(blocks, toc_info)
        # 在干净块上重算偏移（原 parse 时计算的 offset 会被封面碎片干扰，此处修正）
        offset = compute_offset(toc_info.entries, blocks)
        align_pages(toc_info, offset)
        hard = build_hard_injections(toc_info, blocks, found, body_start=body_start)
        toc_nums = {e.number for e in toc_info.entries if e.number}
        soft_confirmed = confirmed - toc_nums  # 已在目录的编号由硬注入负责，避免重复
    soft = build_soft_injections(toc_info, soft_confirmed, found, blocks)
    return apply_injections(blocks, hard + soft)
