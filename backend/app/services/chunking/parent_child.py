"""P1-4 parent-child 切片：检索用精确小块，生成得完整父上下文。

设计：
- 子块：目标 350 tokens（范围 200-500），按 IR 原子边界（段落/列表项/表格行组）贪心合并
- 父块：目标 1000 tokens（范围 700-1600），以 1/2 级 heading 小节为边界
- 父子同表：父块也是 Chunk 行（block_type='parent'），子块 parent_chunk_id 指向它
- token 计数用 tiktoken（cl100k_base）；离线 fake 测试退回 len() 字符近似

入口：`build_parent_child(elements, section_tree) -> list[ParentChildChunk]`
     `ParentChildChunk` 含 content/parent_content/section/page/child_hash/parent_hash
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings

# token 预算（可 .env 调优；首版基线）
CHILD_TARGET_TOKENS = 350
CHILD_MIN_TOKENS = 200
CHILD_MAX_TOKENS = 500
PARENT_TARGET_TOKENS = 1000
PARENT_MIN_TOKENS = 700
PARENT_MAX_TOKENS = 1600


@dataclass
class ParentChildChunk:
    """一个子块 + 其父上下文。"""

    content: str                 # 子块内容（含章节 breadcrumb）
    parent_content: str          # 父块全文（含章节 breadcrumb）
    section: str | None
    page: int | None
    child_hash: str              # 子块正文规范化哈希
    parent_hash: str             # 父块哈希
    block_type: str = "text"     # text / table / formula
    # 单元二 2-2：表格结构化数据（{table_id, columns, rows, row_index}）。
    # 非表格块为 None。table_id 标记同一张表，跨块聚合用；父块不携带（避免重复计数）。
    table_data: dict | None = None


# ---- token 计数 ----
_encoder = None


def _count_tokens(text: str) -> int:
    """token 计数：tiktoken cl100k；settings 标记 fake 时退回字符近似。"""
    global _encoder
    if getattr(settings, "fake_token", False):
        return len(text)
    if _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoder = False  # 标记不可用，退回字符
    if _encoder is False:
        return len(text)
    return len(_encoder.encode(text))


def _norm_hash(text: str) -> str:
    """正文规范化哈希（去空白差异；用于去重/缓存）。"""
    import hashlib
    import re

    return hashlib.sha256(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()


def _section_prefix(section: str | None) -> str:
    return f"## {section}\n" if section else ""


# ---- 从 IR elements / ParsedBlock 提取原子块 ----
def _atoms_from_elements(elements) -> list[dict[str, Any]]:
    """IR elements → 原子块列表。

    每个 atom：{text, section, page, type, level}。
    HEADING 是边界（不单独成 atom，由 section tree 处理）；
    PARAGRAPH/LIST_ITEM/TABLE_ROW/FORMULA 是原子。
    """
    atoms: list[dict[str, Any]] = []
    for el in elements:
        t = getattr(el, "type", None)
        type_name = getattr(t, "value", str(t))
        text = getattr(el, "text", "") or ""
        if not text.strip():
            continue
        if type_name in ("title", "heading"):
            lvl = getattr(el, "heading_level", None)
            if isinstance(lvl, int) and lvl <= 2:
                continue  # 1/2 级标题是边界，不单独成 atom
            # 3 级及以上标题保留为正文（作为切片内容）
        section = " / ".join(getattr(el, "section_path", ())) or None
        page = getattr(el, "page_start", None)
        atoms.append(
            {
                "text": text.strip(),
                "section": section,
                "page": page,
                "type": type_name,
                "level": None,
                # 单元二 2-2：表格结构随原子穿透（table 字段 + 稳定 table_id=element_id）。
                # 落库时 _write_chunks 再前缀 doc.id 命名空间，保证跨文档唯一。
                "table": getattr(el, "table", None),
                "table_id": getattr(el, "element_id", None),
            }
        )
    return atoms


def _atoms_from_blocks(blocks) -> list[dict[str, Any]]:
    """ParsedBlock → 原子块（heading 也参与构建，正文内容不丢失）。"""
    atoms: list[dict[str, Any]] = []
    for b in blocks:
        if not b.text.strip():
            continue
        # section 截断到 300 字符（DB Chunk.section 限制），超长则截断并警告
        section = b.section
        if section and len(section) > 300:
            section = section[:297] + "..."
        atoms.append(
            {
                "text": b.text.strip(),
                "section": section,
                "page": b.page,
                "type": b.block_type or "paragraph",
                "level": None,
            }
        )
    return atoms


def _section_tree_from_blocks(blocks) -> list[dict[str, Any]]:
    """从 blocks 的 heading 建 section tree（IR 不可用时的回退）。

    返回 [{"text": 标题, "level": 1..2, "section_path": "..."}]。
    """
    from app.services.parser.headings import heading_level

    tree: list[dict[str, Any]] = []
    for b in blocks:
        if b.block_type != "heading" or not b.text.strip():
            continue
        # 优先用 section 字段（已由 outline/TOC 校准）
        if b.section:
            parts = [p.strip() for p in b.section.split("/") if p.strip()]
            lvl = len(parts)
            if lvl not in (1, 2):
                continue
            path = " / ".join(parts)
        else:
            lvl = heading_level(b.text)
            if lvl not in (1, 2):
                continue
            path = b.text.strip()
        tree.append({"text": b.text.strip(), "level": lvl, "section_path": path})
    return tree


# ---- 主入口 ----
def build_parent_child(elements, blocks=None) -> list[ParentChildChunk]:
    """从 IR elements（无则 blocks）构建 parent-child 切片。

    - 按 1/2 级 heading 分小节 → 每小节一组原子
    - 组内贪心合并原子到子块上限（200-500 tokens）
    - 小节整体作为父块（700-1600 tokens），超限再拆
    - 表格行组单独处理（不按字符跨行切）
    """
    atoms = _atoms_from_elements(elements) if elements else _atoms_from_blocks(blocks or [])

    # 建 section tree（从 elements 的 heading_level，或 blocks 回退）
    sections: list[dict[str, Any]] = []
    if elements:
        for el in elements:
            t = getattr(el, "type", None)
            type_name = getattr(t, "value", str(t))
            if type_name == "heading" and getattr(el, "heading_level", None) in (1, 2):
                sections.append(
                    {
                        "text": getattr(el, "text", ""),
                        "level": el.heading_level,
                        "section_path": " / ".join(getattr(el, "section_path", ())) or None,
                    }
                )
    else:
        sections = _section_tree_from_blocks(blocks or [])

    # 把 atoms 按当前小节分组（section 变化 = 新小节）
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_sec: str | None = None
    for atom in atoms:
        sec = atom["section"] or cur_sec
        if cur and sec != cur_sec:
            groups.append(cur)
            cur = []
        cur_sec = sec
        cur.append(atom)
    if cur:
        groups.append(cur)

    # 每组分：先切子块，再合成父块
    out: list[ParentChildChunk] = []
    for group in groups:
        section = group[0]["section"]
        page = next((a["page"] for a in group if a["page"]), None)
        out.extend(_chunk_group(group, section, page))
    # P1-2 单元5：过滤噪声 chunk——纯标题重复块（正文==标题）和纯图片引用碎片
    out = [c for c in out if not _is_noise_chunk(c)]
    return out


def _is_noise_chunk(chunk: "ParentChildChunk") -> bool:
    """判断 chunk 是否为噪声（纯标题重复 / 纯图片引用碎片），应被过滤。

    保留表题（表X.Y ...）等有实质内容的分片。
    """
    import re

    body = chunk.content.split("\n", 1)[-1].strip() if "\n" in chunk.content else chunk.content.strip()
    if not body:
        return True
    # 1) 纯标题重复：正文 == section 最后一级标题（如「1 绪论」「习题」「思考题」）
    if chunk.section:
        last = chunk.section.split("/")[-1].strip()
        if body == last:
            return True
    # 2) 纯图片引用：只含「图X.Y」或「(a)/(b)/(c)」等图片编号，无文字
    stripped = re.sub(r"图\d+(?:\.\d+)*", "", body)
    stripped = re.sub(r"^\(\w\)\s*$", "", stripped, flags=re.M)
    stripped = re.sub(r"[\s\n\(\)\[\]（）]+", "", stripped)
    if len(stripped) < 3:
        return True
    return False


def _child_block_type(types: list[str]) -> str:
    """按子块内原子类型判定 block_type（单元 D）。

    - 所有原子都是 table → table（纯表格块）
    - 否则 → text（正文为主；混合正文+表格归 text，避免正文被当表格渲染）
    """
    if types and all(t == "table" for t in types):
        return "table"
    return "text"


def _build_table_data(
    types: list[str], table_ids: list[str | None], tables: list[dict | None]
) -> dict | None:
    """纯表格子块 → 结构化 table_data；非纯表格或跨多张表 → None。

    单张表在当前切片里就是一个子块（atom 不按行拆），rows 取整表数据行、
    row_index=0。table_id 用于跨块聚合（未来大表拆块时按 row_index 拼回完整表）。
    """
    if not types or not all(t == "table" for t in types):
        return None
    distinct = {tid for tid in table_ids if tid}
    if len(distinct) != 1:
        return None  # 一张子块混进多张表 → 不产出结构化数据（避免串列）
    table = next((t for t in tables if t), None)
    if not table:
        return None
    header = table.get("header_path") or []
    rows = table.get("rows") or []
    data_rows = rows[1:] if header and rows and rows[0] == header else rows
    return {
        "table_id": next(iter(distinct)),
        "columns": header,
        "rows": data_rows,
        "row_index": 0,
    }


def _chunk_group(group: list[dict[str, Any]], section: str | None, page: int | None) -> list[ParentChildChunk]:
    """一组原子的子块 + 父块。

    page 修复（P1-2 单元1）：每个子块用「它包含的 atoms 的实际 page」，
    不再统一用 group 首页——跨页 section 的中间页不再被标成首页。
    """
    # 子块：贪心合并到 ≤ CHILD_MAX，同时记录每个子块包含的 atom 的 page 与 type。
    # type 用于每个子块单独判定 block_type（避免「小节里有一个表格，整节正文被连坐标 table」）。
    # 单元 D：表格与正文不混块——遇 table ↔ 非 table 切换先切块，表格自成一纯表格子块
    # （真表格标 table），正文子块保持 text。否则表格行与正文段落合并进同一子块，
    # 纯表格判空失效，真表格也被误标 text。
    # 单元二 2-2：额外追踪每个 atom 的 table/table_id，落库时产出 table_data。
    children: list[dict[str, Any]] = []  # {text, page, types, table_data}
    cur: list[str] = []
    cur_types: list[str] = []
    cur_table_ids: list[str | None] = []
    cur_tables: list[dict | None] = []
    cur_page: int | None = None
    cur_tokens = 0

    def _flush() -> None:
        nonlocal cur, cur_types, cur_table_ids, cur_tables, cur_page, cur_tokens
        if cur:
            children.append(
                {
                    "text": "\n".join(cur),
                    "page": cur_page,
                    "types": list(cur_types),
                    "table_data": _build_table_data(cur_types, cur_table_ids, cur_tables),
                }
            )
        cur = []
        cur_types = []
        cur_table_ids = []
        cur_tables = []
        cur_page = None
        cur_tokens = 0

    for atom in group:
        text = atom["text"]
        tokens = _count_tokens(text)
        atom_type = atom.get("type") or "paragraph"
        # 表格/正文类型切换 → 先切块（表格不跟正文段落合并在同一子块）
        if cur and (cur_types[0] == "table") != (atom_type == "table"):
            _flush()
        # token 超限 → 切块（已满一个最小子块才切，避免碎片）
        if cur and cur_tokens + tokens > CHILD_MAX_TOKENS and cur_tokens >= CHILD_MIN_TOKENS:
            _flush()
        cur.append(text)
        cur_types.append(atom_type)
        cur_table_ids.append(atom.get("table_id") if atom_type == "table" else None)
        cur_tables.append(atom.get("table") if atom_type == "table" else None)
        if cur_page is None:
            cur_page = atom.get("page")
        cur_tokens += tokens
    _flush()

    # 父块：整组作为父（≤ PARENT_MAX），超限按子块拆
    group_text = "\n".join(a["text"] for a in group)
    group_tokens = _count_tokens(group_text)
    if group_tokens <= PARENT_MAX_TOKENS:
        parent_contents = [group_text]
    else:
        # 超长：按子块累积成父块
        parent_contents = []
        buf: list[str] = []
        buf_tokens = 0
        for child in children:
            ct = _count_tokens(child["text"])
            if buf and buf_tokens + ct > PARENT_MAX_TOKENS:
                parent_contents.append("\n".join(buf))
                buf = []
                buf_tokens = 0
            buf.append(child["text"])
            buf_tokens += ct
        if buf:
            parent_contents.append("\n".join(buf))

    prefix = _section_prefix(section)
    results: list[ParentChildChunk] = []
    # 子块 → 父块映射：每个子块归属第一个覆盖它的父块（按累积近似）
    parent_idx = 0
    parent_acc = 0
    parent_lens = [_count_tokens(p) for p in parent_contents]
    for child in children:
        child_text = child["text"]
        # 找到覆盖该子块的父块（累积 token 定位）
        while parent_idx < len(parent_lens) - 1 and parent_acc + _count_tokens(child_text) > parent_lens[parent_idx]:
            parent_acc = 0
            parent_idx += 1
        parent_acc += _count_tokens(child_text)
        parent_text = parent_contents[parent_idx]
        results.append(
            ParentChildChunk(
                content=prefix + child_text,
                parent_content=prefix + parent_text,
                section=section,
                page=child["page"],
                child_hash=_norm_hash(child_text),
                parent_hash=_norm_hash(parent_text),
                # 单元 D：每个子块单独判定 block_type——仅当子块内所有原子都是表格
                # 才标 table，否则归 text。修复「小节含一个表格 → 整节正文连坐标 table」。
                block_type=_child_block_type(child.get("types") or []),
                table_data=child.get("table_data"),
            )
        )
    return results
