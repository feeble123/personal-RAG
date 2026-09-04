"""结构化读表服务（单元二 2-3 + 2-4）：聚合 + 按列取值/筛选/计数 + 精确读表通道。

背景：表格结构已由 2-1（解析层）、2-2（切片穿透 + 落库）存进
`Chunk.table_data = {table_id, columns, rows, row_index}`。本服务把散在多个 chunk 里的
同一张表聚合回完整视图，并提供按列操作的原语。

两层能力：
- 2-3 读表原语：TableView（按列取值/去重/筛选/计数/查值/日期筛选）+ load_table/list_table_ids
- 2-4 精确通道：query_table——识别「计数/枚举/查值」问句，找最匹配的表读出精确答案；
  找不到强匹配（实体无落点、无名称列、答案为空）返回 None，由调用方回退向量检索。

查询是**精确读**（直接按列名/值匹配），不走向量/BM25——这是「看懂表格列」后
"列出来/数一下/筛出来" 类问题不再靠向量猜的关键。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document


def _norm_col(name: str) -> str:
    """列名归一：去全部空白（「名 称」→「名称」、「备 注」→「备注」）。"""
    return re.sub(r"\s+", "", (name or "").strip())


def _norm_cell(value) -> str:
    """单元格值归一：去首尾空白 + 折叠内部空白（值匹配时容空格差异）。"""
    return re.sub(r"\s+", "", str(value or "").strip())


@dataclass(frozen=True)
class TableView:
    """一张表的完整结构化视图（列名 + 数据行，跨块聚合后）。

    数据行已对齐列宽（每行长度 == 列数），不含表头。所有按列操作都先做列名归一化，
    容「名 称」这类带空格的表头与查询里的「名称」对齐。

    section/source/chunk_ids 是 2-4 精确通道定位引用用的元数据（本表来自哪个文档、
    哪些切片），非查询原语；纯函数层（_aggregate）不填，由 load_table 从 DB 补上。
    """

    table_id: str
    columns: list[str]
    rows: list[list[str]]
    section: str | None = None
    source: str | None = None
    chunk_ids: tuple[int, ...] = ()
    doc_id: int | None = None
    kb_id: int | None = None

    def column_index(self, column: str) -> int | None:
        target = _norm_col(column)
        for i, c in enumerate(self.columns):
            if _norm_col(c) == target:
                return i
        return None

    def column_values(self, column: str) -> list[str]:
        """取一整列的值（按行顺序，含重复）。"""
        idx = self.column_index(column)
        if idx is None:
            return []
        return [row[idx] for row in self.rows]

    def unique_values(self, column: str) -> list[str]:
        """取一整列的去重值（保持首次出现顺序）。"""
        seen: set[str] = set()
        out: list[str] = []
        for v in self.column_values(column):
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def filter_rows(self, column: str, value) -> list[list[str]]:
        """筛选：column 列的值 == value 的所有行（值归一化后比较）。"""
        idx = self.column_index(column)
        if idx is None:
            return []
        target = _norm_cell(value)
        return [row for row in self.rows if _norm_cell(row[idx]) == target]

    def count(self, column: str, value) -> int:
        """计数：column 列的值 == value 的行数。"""
        return len(self.filter_rows(column, value))

    def lookup(self, where_column: str, where_value, get_column: str) -> list[str]:
        """查值：where_column == where_value 的行里取 get_column 的值（可能多行）。

        例：lookup("名称", "动力配电箱", "数量") → ["3"]。
        """
        gi = self.column_index(get_column)
        if gi is None:
            return []
        return [row[gi] for row in self.filter_rows(where_column, where_value)]

    def filter_date_after(self, column: str, iso_date: str) -> list[list[str]]:
        """日期筛选：column 列值（ISO YYYY-MM-DD）> iso_date 的行。

        依赖 2-3 前置（单元二③）已把日期统一成 YYYY-MM-DD——ISO 串的字典序即时间序，
        无需解析日期对象。
        """
        idx = self.column_index(column)
        if idx is None:
            return []
        return [row for row in self.rows if _norm_cell(row[idx]) > iso_date]

    def rows_matching_value(self, value) -> list[list[str]]:
        """找任意单元格值 == value 的行（跨列匹配，供实体定位用）。"""
        target = _norm_cell(value)
        return [row for row in self.rows if any(_norm_cell(c) == target for c in row)]


def _aggregate(table_id: str, chunks: list[Chunk]) -> TableView | None:
    """把同一 table_id 的多个 chunk 拼回完整表（按 row_index 排序，对齐列宽）。

    当前一张表通常就是一个 chunk（整表行全量），此聚合为未来大表拆块预留：
    拆块后各行按 row_index 拼回、列宽对齐，查询逻辑不变。
    """
    columns: list[str] | None = None
    parts: list[tuple[int, list[list[str]]]] = []
    for c in chunks:
        td = c.table_data
        if not td:
            continue
        cols = td.get("columns") or []
        rows = td.get("rows") or []
        if columns is None:
            columns = list(cols)
        parts.append((int(td.get("row_index", 0) or 0), rows))
    if columns is None:
        return None
    parts.sort(key=lambda x: x[0])
    width = len(columns)
    merged: list[list[str]] = []
    for _, rows in parts:
        for r in rows:
            r = list(r)
            if len(r) < width:
                r = r + [""] * (width - len(r))
            merged.append(r[:width])
    return TableView(table_id=table_id, columns=columns, rows=merged)


async def _active_version_ids(db: AsyncSession, kb_id: int | None = None) -> set[int]:
    """当前 active 的文档版本 id 集合（读表只认已发布版本，retired 不可作为数据源）。

    与 rag._active_version_ids 同语义；这里单独一份，避免 table_query 反向 import rag
    （rag 会 import 本模块走精确通道，方向倒过来就是循环导入）。
    """
    stmt = select(Document.active_version_id).where(Document.active_version_id.is_not(None))
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    return {r[0] for r in (await db.execute(stmt)).all()}


async def load_table(
    db: AsyncSession, table_id: str, kb_id: int | None = None
) -> TableView | None:
    """按 table_id 聚合 active 版本的所有表格切片 → TableView（找不到返回 None）。

    除列名/数据行外，还从切片带回 section（含 sheet 名）/ source（文件名）/ chunk_ids，
    供 2-4 精确通道定位引用。
    """
    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.doc_id == Document.id)
        .where(Chunk.table_data.is_not(None))
    )
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    active_ids = await _active_version_ids(db, kb_id)
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    rows = (await db.execute(stmt)).all()
    matched = [(c, d) for c, d in rows if (c.table_data or {}).get("table_id") == table_id]
    if not matched:
        return None
    tv = _aggregate(table_id, [c for c, _ in matched])
    if tv is None:
        return None
    section = next((c.section for c, _ in matched if c.section), None)
    source = next((d.filename for _, d in matched if d.filename), None)
    chunk_ids = tuple(c.id for c, _ in matched)
    doc_id = next((d.id for _, d in matched), None)
    kb_id = next((c.kb_id for c, _ in matched), None)
    return replace(tv, section=section, source=source, chunk_ids=chunk_ids, doc_id=doc_id, kb_id=kb_id)


async def list_table_ids(db: AsyncSession, kb_id: int | None = None) -> list[str]:
    """列出库里所有表格的 table_id（去重，供 2-4 找表用）。"""
    stmt = select(Chunk).where(Chunk.table_data.is_not(None))
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    active_ids = await _active_version_ids(db, kb_id)
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    chunks = (await db.scalars(stmt)).all()
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        tid = (c.table_data or {}).get("table_id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


# ---- 单元二 2-4：精确读表通道（意图命中「读表」后调用）----
# 核心原则：这是「精确辅助通道」，只有**强匹配 + 能精确读出答案**才接管检索；
# 任何一步不确定都返回 None，由调用方回退向量检索（零回归）。

# 名称列判定：列名以「名称」结尾，或是这几类「主体名」列（设备/方案/项目/材料…）
_NAME_COLUMN_EXACT = {"名称", "方案", "项目", "工程", "设备", "材料", "措施", "内容", "类别", "类型"}
# 计数/数量列判定：数值答在这列里
_COUNT_COLUMN = {"数量", "个数", "台数", "套数", "件数", "总量", "合计"}
# 枚举过滤时排除的「泛词」：这类词命中名称列值不当作过滤条件（「方案」不是具体实体）
_GENERIC_FILTER_WORDS = {
    "方案", "项目", "工程", "措施", "内容", "设备", "材料", "名称", "制度", "台账",
    "管理", "施工", "体系", "预案", "清单", "一览表", "汇总",
}
# 「要数的单位」泛词：问「一共多少 X」时，X 是计量单位，不是要查找的具体实体。
# 这类词**永不**当作 _find_subject_value 的查找对象，也不参与实体落点门禁——
# 否则「一共多少方案」会把「方案」当成具体名去匹配第一个方案名（如「临建方案」）。
# 只放「可数的东西」（方案/设备/项目…）；容器词（台账/文档/文件）另见 _CONTAINER_WORDS。
_GENERIC_UNIT_WORDS = {
    "方案", "设备", "项目", "工程", "措施", "材料", "内容",
}
# 容器/文档词：指代整份文件（「这份台账」）而非某张表，**不作** sheet 定位信号——
# 否则「这份台账中一共多少方案」里的「台账」会子串命中「备案版方案台账」sheet 名，
# 被误判成「点名了备案版那张表」，只数 36 而非整份 40。
_CONTAINER_WORDS = {"台账", "文档", "文件"}
# 问句框架词/量词/语气词：jieba 分词后剔除，剩下的才是实体/主题词。
# 单元二 2-4 补：输出格式/元指令词（请以/表格/形式/输出/不用/关心…）是「怎么答」，
# 不是「问什么」——混进内容词会污染表匹配分。真正的否定对象（交底/时间这类领域词）
# 由 _question_clauses 在更上层按「只保留问句」丢弃，不在这里黑名单化（否则误伤正经列名）。
_QUERY_STOPWORDS = {
    "数量", "多少", "几个", "几台", "几套", "几座", "几处", "个数", "台数", "套数", "共计", "合计",
    "有哪些", "哪些", "列出", "清单", "一览", "名单", "所有", "全部", "都有", "都包含",
    "包含", "包括", "什么", "请问", "请", "的", "是", "吗", "呢", "如何", "怎么", "怎样",
    "有", "几", "个", "台", "套", "座", "中", "里", "内", "哪些", "谁", "哪里", "哪",
    "您好", "你好", "帮我", "查", "查询",
    # 元指令/输出格式词（「请以表格输出」「不用…」「我不关心…」）
    "一共", "总共", "这份", "请以", "表格", "形式", "输出", "给我", "不用", "不要",
    "关心", "麻烦", "需要", "别", "这", "那",
}


def _find_column(tv: TableView, predicate) -> int | None:
    """按谓词找列下标（名称列/数量列等）；找不到返回 None。"""
    for i, c in enumerate(tv.columns):
        if predicate(c):
            return i
    return None


def _is_name_column(col: str) -> bool:
    c = _norm_col(col)
    if not c:
        return False
    if c in _NAME_COLUMN_EXACT:
        return True
    return c.endswith("名称")


def _is_count_column(col: str) -> bool:
    return _norm_col(col) in _COUNT_COLUMN


def _content_words(query: str) -> list[str]:
    """jieba 分词 + 剔除框架词/量词 → 查询里的「内容词」（实体/主题，按出现顺序）。"""
    import jieba  # 懒加载（首次 import 慢，且只在精确通道命中时用）

    words: list[str] = []
    for w in jieba.lcut(query):
        w = w.strip()
        if len(w) < 2 or w in _QUERY_STOPWORDS:
            continue
        if w not in words:
            words.append(w)
    return words


# 问句信号（多少/有哪些/数量…）：用来判断一个子句是不是「问句本体」。
_QUESTION_SIGNAL_RE = re.compile(
    r"多少|几台|几套|几座|几个|几处|数量|个数|台数|套数|共计|合计|"
    r"有哪些|哪些|列出|清单|一览|名单|所有|全部|都有|都包含|几级|几种|几类"
)
# 句末标点（切分子句用）
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!；;\n]")


def _question_core(query: str) -> str:
    """只留「问句本体」，丢弃输出格式/否定约束等元子句。

    用户常在问句后追加「请以表格输出」「不用把××输出」「我不关心××」这类元指令——
    这些是「怎么回答」不是「问什么」，混进内容词会污染表匹配（如「交底/时间」虚高分）。
    按句末标点切分，只保留含问句信号（多少/有哪些/数量…）的子句。
    """
    q = (query or "").strip()
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(q) if c.strip()]
    if not clauses:
        return q
    core = [c for c in clauses if _QUESTION_SIGNAL_RE.search(c)]
    return "，".join(core) if core else q


def _find_subject_value(tv: TableView, words: list[str]) -> tuple[int | None, str | None]:
    """在表里找「问句点名的实体值」（计数/查值的主题）。

    优先在名称列里找（设备名/方案名等实体通常落在名称列），找不到再全表扫。
    双向匹配：单元格值 ⊇ 内容词（「动力配电箱」精确命中）或 内容词 ⊇ 单元格值。
    返回 (列下标, 命中的单元格值)；找不到返回 (None, None)。

    单元二 2-4 修复：跳过「泛词/单位词」（方案/设备/项目/制度/体系…）——它们是
    「要数的单位」或「分类」，不是具体实体。否则「一共多少方案」会把「方案」当主体，
    子串命中第一个方案名（「临建方案」）只数出 1。
    """
    name_col = _find_column(tv, _is_name_column)
    order = ([name_col] if name_col is not None else []) + [
        i for i in range(len(tv.columns)) if i != name_col
    ]
    skip = _GENERIC_FILTER_WORDS | _GENERIC_UNIT_WORDS
    for w in words:
        if w in skip:
            continue
        for col_idx in order:
            for row in tv.rows:
                v = _norm_cell(row[col_idx])
                if len(v) < 2:
                    continue
                if v in w or w in v:
                    return col_idx, v
    return None, None


def _word_present(w: str, tv: TableView) -> bool:
    """内容词是否在表里「有落点」（列名 / 单元格值 / sheet 名 / 文件名，任一命中）。"""
    if any(_norm_col(c) and (w in _norm_col(c) or _norm_col(c) in w) for c in tv.columns):
        return True
    if any(
        len(_norm_cell(cell)) >= 2 and (w in _norm_cell(cell) or _norm_cell(cell) in w)
        for row in tv.rows for cell in row
    ):
        return True
    for seg in re.split(r"[/\\]", _norm_col(tv.section or "")):
        if seg and w in seg:
            return True
    src_stem = re.sub(r"\.[^.]+$", "", _norm_col(tv.source or ""))
    return bool(src_stem and w in src_stem)


def _table_score(words: list[str], tv: TableView) -> float:
    """表与查询的匹配分（越高越可能答这道题）。

    权重设计（按「用户意图的明确程度」排）：
    - sheet 名 / section 命中 +8：用户点名 sheet（如「制度体系」）是最强信号，必须压过行数
    - 文件名命中 +3
    - 列名命中 +3（**去重**：一个词命中再多的列也只 +3 一次——否则「时间」命中 5 个
      「××完成时间」列 +15，把无关表顶成最优，如「不用把交底时间输出」带偏表选择）
    - 单元格值命中 +2（**去重**，每个词最多 +2，不按行数累加——否则 36 行的「备案版」大表
      靠「方案」一词碾压 4 行的「制度体系」小表，把用户点名的 sheet 挤掉）
    分数为 0 说明表与问题毫无交集，不可能是答案表。
    """
    score = 0.0
    col_norms = [_norm_col(c) for c in tv.columns]
    src_stem = re.sub(r"\.[^.]+$", "", _norm_col(tv.source or ""))
    section_parts = re.split(r"[/\\]", _norm_col(tv.section or ""))
    for w in words:
        if len(w) < 2:
            continue
        for seg in section_parts:
            if seg and w in seg:
                score += 8.0
        if src_stem and w in src_stem:
            score += 3.0
        if any(cn and (w in cn or cn in w) for cn in col_norms):
            score += 3.0
        if any(
            len(_norm_cell(cell)) >= 2 and (w in _norm_cell(cell) or _norm_cell(cell) in w)
            for row in tv.rows for cell in row
        ):
            score += 2.0
    return score


@dataclass(frozen=True)
class TableAnswer:
    """一次精确读表的结构化答案（2-4）。

    kind: count（计数/查值）/ enum（枚举/清单）
    answer_text: 已格式化的答案正文（直接进 snippet，供 LLM 生成）
    rows: 命中的原始数据行（全量，供 LLM 看全貌）
    table_id / source / section / chunk_ids: 引用定位（rag 层据此构造 RetrievedChunk）
    """

    kind: str
    subject: str
    answer_text: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    table_id: str
    source: str
    section: str | None
    chunk_ids: tuple[int, ...]
    doc_id: int | None
    kb_id: int | None


def _answer_count(tv: TableView, query: str, words: list[str]) -> TableAnswer | None:
    """计数/查值：定位实体值 → 命中的行 → 读出数量列的值（或命中行数）。"""
    col_idx, subj_val = _find_subject_value(tv, words)
    if col_idx is None or subj_val is None:
        return None
    name_col = tv.columns[col_idx]
    count_col = _find_column(tv, _is_count_column)
    matching = tv.filter_rows(name_col, subj_val)
    if not matching:
        return None
    if count_col is not None and len(matching) == 1:
        number = matching[0][count_col] if count_col < len(matching[0]) else ""
        number = number or str(len(matching))
    else:
        number = str(len(matching))
    answer = f"{subj_val} 数量: {number}"
    return TableAnswer(
        kind="count", subject=subj_val, answer_text=answer,
        columns=tuple(tv.columns), rows=tuple(tuple(r) for r in matching),
        table_id=tv.table_id, source=tv.source or "", section=tv.section,
        chunk_ids=tv.chunk_ids, doc_id=tv.doc_id, kb_id=tv.kb_id,
    )


def _sheet_name(tv: TableView) -> str:
    """取 section 的 sheet 名（最后一段）：「已报送方案台账.xlsx / 制度体系」→「制度体系」。"""
    parts = [p.strip() for p in re.split(r"[/\\]", tv.section or "") if p.strip()]
    return parts[-1] if parts else ""


def _answer_total(views: list[TableView], query: str, words: list[str]) -> TableAnswer | None:
    """「一共多少 X」求总数（无具体实体的计数，单元二 2-4 修复新增）。

    与 `_answer_count`（点名实体计数，如「动力配电箱有几台」）区分：
    - 找「单位词」X（方案/设备/项目…，命中某表的列名/sheet/文件名）
    - 点名具体 sheet → 只数那张表；否则同一文档的 X 表行数求和（一份台账多 sheet）
    - 找不到单位词 / 范围不明确 → None（回退向量，绝不猜）
    """
    # 1) 单位词：泛词里，哪个在任一表里有落点（列名「方案名称」/sheet「设备表」/文件名）
    unit = next(
        (
            w
            for w in words
            if w in _GENERIC_UNIT_WORDS and any(_word_present(w, v) for v in views)
        ),
        None,
    )
    if unit is None:
        return None
    eligible = [v for v in views if _word_present(unit, v)]
    if not eligible:
        return None
    # 2) 点名 sheet：除单位词/容器词外的内容词，恰好命中一张表的 sheet 名 → 只数那张表。
    # 容器词（台账/文档/文件）指整份文件，不是 sheet 定位信号（「这份台账」≠「备案版方案台账」）。
    scope: TableView | None = None
    for w in words:
        if w == unit or w in _CONTAINER_WORDS:
            continue
        sheet_hits = [v for v in eligible if w in _sheet_name(v)]
        if len(sheet_hits) == 1:
            scope = sheet_hits[0]
            break
    # 3) 范围：点名 sheet 用那张表；否则同一文档的 eligible 表求和（一份台账多 sheet）
    if scope is not None:
        targets = [scope]
    else:
        anchor = max(eligible, key=lambda v: _table_score(words, v))
        targets = [v for v in eligible if v.doc_id == anchor.doc_id]
    total = sum(len(v.rows) for v in targets)
    if total == 0:
        return None
    detail = " + ".join(f"{_sheet_name(v)} {len(v.rows)}" for v in targets)
    answer = f"{unit} 总数: {total}" if len(targets) == 1 else f"{unit} 总数: {total}（{detail}）"
    rows = tuple(tuple(r) for v in targets for r in v.rows)
    return TableAnswer(
        kind="count", subject=unit, answer_text=answer,
        columns=tuple(targets[0].columns), rows=rows,
        table_id=targets[0].table_id, source=targets[0].source or "",
        section=targets[0].section, chunk_ids=targets[0].chunk_ids,
        doc_id=targets[0].doc_id, kb_id=targets[0].kb_id,
    )


def _answer_enum(tv: TableView, query: str, words: list[str]) -> TableAnswer | None:
    """枚举/清单：取名称列的（去重）整列值；有具体实体词时按它过滤。"""
    name_col = _find_column(tv, _is_name_column)
    if name_col is None:
        return None
    rows = tv.rows
    filter_word = None
    # 过滤：找一个「具体」实体词（≥3 字且非泛词）命中名称列值的行
    for w in words:
        if len(w) < 3 or w in _GENERIC_FILTER_WORDS:
            continue
        sub = [r for r in tv.rows if w in _norm_cell(r[name_col])]
        if sub:
            rows = sub
            filter_word = w
            break
    values: list[str] = []
    for r in rows:
        v = r[name_col] if name_col < len(r) else ""
        if v and v not in values:
            values.append(v)
    if not values:
        return None
    answer = "、".join(values)
    if filter_word:
        answer = f"{filter_word} 相关: {answer}"
    return TableAnswer(
        kind="enum", subject=tv.columns[name_col], answer_text=answer,
        columns=tuple(tv.columns), rows=tuple(tuple(r) for r in rows),
        table_id=tv.table_id, source=tv.source or "", section=tv.section,
        chunk_ids=tv.chunk_ids, doc_id=tv.doc_id, kb_id=tv.kb_id,
    )


async def query_table(
    db: AsyncSession, query: str, kb_id: int | None = None
) -> TableAnswer | None:
    """精确读表入口（2-4）：识别读表问题 → 找最匹配的表 → 读出精确答案。

    返回 None 表示「库里没有可答的表 / 无法精确读出答案」→ 调用方回退向量检索。
    这是**强门禁**：分数不够、没有名称列、答案为空，一律返回 None，绝不猜。
    """
    from app.services.intent import table_query_kind

    kind = table_query_kind(query)
    if kind is None:
        return None
    table_ids = await list_table_ids(db, kb_id)
    if not table_ids:
        return None
    views: list[TableView] = []
    for tid in table_ids:
        tv = await load_table(db, tid, kb_id)
        if tv is not None and tv.columns and tv.rows:
            views.append(tv)
    if not views:
        return None
    # 只保留问句本体：丢弃「请以表格输出」「不用把交底时间输出」等元指令子句（污染打分）
    core = _question_core(query)
    words = _content_words(core)
    if not words:
        return None
    scored = sorted(((v, _table_score(words, v)) for v in views), key=lambda x: x[1], reverse=True)
    best, best_score = scored[0]
    if best_score < 1.0:
        return None  # 表与问题无交集 → 不接管，回退向量
    # 实体落点门禁：问句里的「点名实体」（≥3 字、非泛词、非单位词、非框架词）若在最优表里
    # 毫无落点，说明这张表答不了这个具体实体（如「肖家湾水厂项目有哪些报送方案」里的
    # 「肖家湾」）——直接回退向量检索，绝不把整列 dump 出来冒充答案。
    for w in words:
        if (
            len(w) >= 3
            and w not in _GENERIC_FILTER_WORDS
            and w not in _GENERIC_UNIT_WORDS
            and w not in _QUERY_STOPWORDS
        ):
            if not _word_present(w, best):
                return None
    if kind == "count":
        # 点名实体计数（「动力配电箱有几台」）优先；定位不到具体实体时走「求总数」
        # （「一共多少方案」→ 整份文档行数求和）。求总数也返回 None 时回退向量。
        ans = _answer_count(best, query, words)
        if ans is not None:
            return ans
        return _answer_total(views, query, words)
    return _answer_enum(best, query, words)
