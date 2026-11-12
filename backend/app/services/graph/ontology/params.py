"""参数先验：Excel 读取 → 校验 → 转成实例草稿。

**这里不产生任何数值。** 参数范围、初值全部来自人工填写的 Excel；
留空的就保持空，导入只给 warning，绝不替用户猜一个数字。

Excel 列（顺序无关，按表头识别）：

    参数名 | 中文名 | 分组 | 单位 | 物理下限 | 物理上限 | 通用下限 | 通用上限
    条件范围 | 初值 | 敏感度 | 依据类型 | 依据 | 备注

条件范围写法（多条换行或用 | 分隔）：

    climate=湿润;flood_type=单峰 => 0.15~0.30

含义：当流域属于「湿润 + 单峰」时，该参数的推荐范围是 0.15~0.30。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Protocol

from app.services.graph.ontology.instances import InstanceDraft
from app.services.graph.ontology.registry import OntologyRegistry
from app.services.graph.ontology.validator import validate_entity

ENTITY_TYPE = "Parameter"

# 表头 → 内部字段（顺序即模板列顺序）
COLUMNS: tuple[tuple[str, str], ...] = (
    ("参数名", "name"),
    ("中文名", "display_zh"),
    ("分组", "group"),
    ("单位", "unit"),
    ("物理下限", "physical_low"),
    ("物理上限", "physical_high"),
    ("通用下限", "range_low"),
    ("通用上限", "range_high"),
    ("条件范围", "condition_ranges"),
    ("初值", "initial"),
    ("敏感度", "sensitivity"),
    ("依据类型", "evidence_type"),
    ("依据", "evidence_note"),
    ("依据文档", "evidence_doc"),
    ("依据位置", "evidence_locator"),
    ("原文摘录", "evidence_snippet"),
    ("备注", "note"),
)
HEADERS: tuple[str, ...] = tuple(h for h, _ in COLUMNS)

_NUMERIC_FIELDS = ("physical_low", "physical_high", "range_low", "range_high", "initial")
_BLANK_MARKERS = {"", "-", "—", "?", "？", "待确认", "n/a", "na", "none", "null"}


@dataclass
class PriorRow:
    """Excel 的一行（已解析成字段，数值还没校验）。"""

    row_no: int
    name: str = ""
    display_zh: str = ""
    group: str = ""
    unit: str = ""
    physical_low: Any = None
    physical_high: Any = None
    range_low: Any = None
    range_high: Any = None
    condition_ranges: str = ""
    initial: Any = None
    sensitivity: str = ""
    evidence_type: str = ""
    evidence_note: str = ""
    evidence_doc: str = ""
    evidence_locator: str = ""
    evidence_snippet: str = ""
    note: str = ""


@dataclass
class PriorBuildResult:
    drafts: list[InstanceDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rows: list[PriorRow] = field(default_factory=list)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_number(value: Any, where: str) -> tuple[float | None, str | None]:
    """把单元格转成数字；空/待确认 → None；非法 → (None, 告警)。"""
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    text = _cell_text(value)
    if text.lower() in _BLANK_MARKERS:
        return None, None
    try:
        return float(text), None
    except ValueError:
        return None, f"{where}：{text!r} 不是数字，已忽略"


def parse_conditions(text: str, where: str) -> tuple[list[dict[str, Any]], list[str]]:
    """解析条件范围，返回 [{low, high, when:{...}}, ...]。"""
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    raw = (text or "").replace("\r", "\n")
    chunks = [c.strip() for c in raw.replace("|", "\n").split("\n") if c.strip()]
    for chunk in chunks:
        if "=>" not in chunk and "＝>" not in chunk and "->" not in chunk:
            warnings.append(f"{where}：条件范围 {chunk!r} 缺少 '=>'，已忽略")
            continue
        left, right = _split_arrow(chunk)
        when: dict[str, str] = {}
        for pair in left.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                warnings.append(f"{where}：条件 {pair!r} 缺少 '='，已忽略该条")
                when = {}
                break
            key, val = pair.split("=", 1)
            when[key.strip()] = val.strip()
        if not when:
            continue
        low, high, err = _parse_range(right)
        if err:
            warnings.append(f"{where}：{err}")
            continue
        out.append({"low": low, "high": high, "when": when})
    return out, warnings


def _split_arrow(chunk: str) -> tuple[str, str]:
    for arrow in ("=>", "＝>", "->"):
        if arrow in chunk:
            left, right = chunk.split(arrow, 1)
            return left.strip(), right.strip()
    return chunk, ""


def _parse_range(text: str) -> tuple[float | None, float | None, str | None]:
    raw = text.strip().replace("～", "~").replace("—", "~")
    if "~" not in raw:
        try:
            value = float(raw)
            return value, value, None
        except ValueError:
            return None, None, f"范围 {text!r} 无法解析（应为 下限~上限）"
    low_s, high_s = raw.split("~", 1)
    try:
        low = float(low_s.strip()) if low_s.strip() else None
        high = float(high_s.strip()) if high_s.strip() else None
    except ValueError:
        return None, None, f"范围 {text!r} 里有非数字"
    if low is not None and high is not None and low > high:
        return low, high, f"范围 {text!r} 下限大于上限，请检查"
    return low, high, None


_PAGE_RE = re.compile(r"(?:第\s*)?(\d{1,4})\s*(?:页|頁)|[pP]\s*\.?\s*(\d{1,4})")


def extract_page(locator: str) -> int | None:
    """从「第53页 / P53 / 2.3节 第53页」里取出页码；取不到返回 None。"""
    if not locator:
        return None
    match = _PAGE_RE.search(locator)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _build_evidence_item(row: PriorRow) -> list[dict[str, Any]]:
    """把一行的依据列组成**结构化证据**（能溯源就带 doc/chunk，不能就明确标出来）。"""
    note = _cell_text(row.evidence_note)
    doc_name = _cell_text(row.evidence_doc)
    if not note and not doc_name:
        return []
    item: dict[str, Any] = {
        "type": _cell_text(row.evidence_type) or "未标注",
        "note": note,
        # 默认未溯源；导入时会（如果找得到）补上 doc_id / chunk_id
        "resolved_by": "none",
        "traceable": False,
    }
    if doc_name:
        item["doc_name"] = doc_name
    locator = _cell_text(row.evidence_locator)
    if locator:
        item["locator"] = locator
        page = extract_page(locator)
        if page is not None:
            item["page"] = page
    snippet = _cell_text(row.evidence_snippet)
    if snippet:
        item["snippet"] = snippet
    return [item]


def read_rows(path: str | Path) -> list[PriorRow]:
    """读 Excel。表头按名称识别，列顺序可调整；没有「参数名」的表头直接报错。"""
    from openpyxl import load_workbook  # 延迟导入：不导入参数表时不加载

    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"参数表不存在：{file}")
    wb = load_workbook(file, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if header is None:
            return []
        header_map = {_cell_text(h): idx for idx, h in enumerate(header) if _cell_text(h)}
        if "参数名" not in header_map:
            raise ValueError(f"{file.name} 第一行缺少「参数名」列，无法识别参数表")

        out: list[PriorRow] = []
        for row_no, values in enumerate(rows_iter, start=2):
            item = PriorRow(row_no=row_no)
            for header_name, field_name in COLUMNS:
                idx = header_map.get(header_name)
                if idx is None or idx >= len(values):
                    continue
                setattr(item, field_name, values[idx])
            name = _cell_text(item.name)
            if not name or name.startswith("#") or name.startswith("示例"):
                continue  # 空行、注释行、示例行
            item.name = name
            out.append(item)
        return out
    finally:
        wb.close()


def build_drafts(
    rows: Iterable[PriorRow],
    registry: OntologyRegistry,
    *,
    profile_id: str,
    kb_id: int = 0,
    source: str = "import",
) -> PriorBuildResult:
    """行 → 实例草稿。校验不过的行记 error 并跳过，其余照常。"""
    result = PriorBuildResult()
    for row in rows:
        result.rows.append(row)
        where = f"第 {row.row_no} 行 {row.name or '(未填参数名)'}"
        props: dict[str, Any] = {}
        display_zh = _cell_text(row.display_zh)
        if display_zh:
            props["display_zh"] = display_zh
        group = _cell_text(row.group)
        if group:
            props["group"] = group
        unit = _cell_text(row.unit)
        if unit:
            props["unit"] = unit
        sensitivity = _cell_text(row.sensitivity)
        if sensitivity:
            props["sensitivity"] = sensitivity
        note = _cell_text(row.note)
        if note:
            props["note"] = note

        has_number = False
        for field_name in _NUMERIC_FIELDS:
            value, warn = parse_number(getattr(row, field_name), f"{where} 的「{field_name}」")
            if warn:
                result.warnings.append(warn)
            if value is not None:
                props[field_name] = value
                has_number = True

        ranges, warns = parse_conditions(_cell_text(row.condition_ranges), where)
        result.warnings.extend(warns)

        check = validate_entity(registry, ENTITY_TYPE, row.name, props)
        if not check.ok:
            result.errors.extend(f"{where}：{e}" for e in check.errors)
            continue
        result.warnings.extend(f"{where}：{w}" for w in check.warnings)

        evidence = _build_evidence_item(row)
        if not evidence and (has_number or ranges):
            result.warnings.append(
                f"{where}：填了数值但没有「依据」，推荐卡不会输出这些数值（不变量：无证据不输出）"
            )

        result.drafts.append(
            InstanceDraft(
                profile_id=profile_id,
                entity_type=ENTITY_TYPE,
                name=row.name,
                uid=registry.uid_for(ENTITY_TYPE, row.name, kb_id),
                kb_id=kb_id,
                props=props,
                ranges=ranges,
                evidence=evidence,
                source=source,
            )
        )
    return result


def load_prior_excel(
    path: str | Path,
    registry: OntologyRegistry,
    *,
    profile_id: str,
    kb_id: int = 0,
) -> PriorBuildResult:
    """读表 + 转草稿（一步到位，导入脚本用）。"""
    return build_drafts(
        read_rows(path), registry, profile_id=profile_id, kb_id=kb_id, source="import"
    )


# ---------------- 证据溯源 ----------------
# 目标：让「依据」不再是随手写的一句话，而是能点回原文的引用。
# 做不到就明确标出来（resolved_by=none），绝不假装有出处。

class EvidenceLookup(Protocol):
    """查文档/切片的接口（便于单测注入假实现）。"""

    async def find_document(self, name: str, kb_id: int) -> dict[str, Any] | None: ...

    async def find_chunk(
        self, doc_id: int, page: int | None, snippet: str | None
    ) -> int | None: ...


class DbEvidenceLookup:
    """按文档名/页码/原文摘录在知识库里定位（只读查询）。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def find_document(self, name: str, kb_id: int) -> dict[str, Any] | None:
        from sqlalchemy import select

        from app.db.models import Document

        stmt = select(Document).where(Document.filename == name)
        if kb_id:
            stmt = stmt.where(Document.kb_id == kb_id)
        exact = (await self._session.execute(stmt.limit(2))).scalars().all()
        if len(exact) == 1:
            doc = exact[0]
            return {"id": doc.id, "filename": doc.filename, "kb_id": doc.kb_id}

        fuzzy_stmt = select(Document).where(Document.filename.ilike(f"%{name}%"))
        if kb_id:
            fuzzy_stmt = fuzzy_stmt.where(Document.kb_id == kb_id)
        fuzzy = (await self._session.execute(fuzzy_stmt.limit(2))).scalars().all()
        if len(fuzzy) == 1:
            doc = fuzzy[0]
            return {"id": doc.id, "filename": doc.filename, "kb_id": doc.kb_id}
        return None

    async def find_chunk(
        self, doc_id: int, page: int | None, snippet: str | None
    ) -> int | None:
        from sqlalchemy import select

        from app.db.models import Chunk

        base = select(Chunk.id).where(Chunk.doc_id == doc_id)
        page_stmt = base if page is None else base.where(Chunk.page == page)
        stmt = page_stmt
        if snippet:
            # 取摘录里最长的一段连续文字去匹配，避免标点/空白差异导致匹配失败。
            # 先按页码过滤找，找不到再放开页码（PDF 印刷页码与解析器页码常有偏移），
            # 但仍然要求正文出现该短语——宁可找不到，也不随便指一个切片冒充出处。
            probe = snippet.strip()
            for statement in (page_stmt, base):
                for cut in (80, 40, 20):
                    piece = probe[:cut]
                    if len(piece) < 6:
                        continue
                    hit = (
                        await self._session.execute(
                            statement.where(Chunk.content.contains(piece)).limit(1)
                        )
                    ).scalars().first()
                    if hit is not None:
                        return int(hit)
            return None
        if page is not None:
            hit = (await self._session.execute(stmt.order_by(Chunk.chunk_index).limit(1))).scalars().first()
            return int(hit) if hit is not None else None
        return None


async def resolve_evidence_with(
    result: PriorBuildResult, lookup: EvidenceLookup, *, kb_id: int = 0
) -> PriorBuildResult:
    """逐条把证据的文档名/页码解析成 doc_id / chunk_id（找不到就标未溯源）。"""
    resolved_drafts = []
    for draft in result.drafts:
        if not draft.evidence:
            resolved_drafts.append(draft)
            continue
        items: list[dict[str, Any]] = []
        for raw in draft.evidence:
            item = dict(raw)
            doc_name = str(item.get("doc_name") or "").strip()
            if not doc_name:
                item["resolved_by"] = "none"
                item["traceable"] = False
                items.append(item)
                continue
            doc = await lookup.find_document(doc_name, kb_id)
            if doc is None:
                item["resolved_by"] = "none"
                item["traceable"] = False
                result.warnings.append(
                    f"{draft.name}：依据文档「{doc_name}」在知识库里找不到，已标记为未溯源"
                )
                items.append(item)
                continue
            item["doc_id"] = doc["id"]
            item["doc_name"] = doc["filename"]
            chunk_id = await lookup.find_chunk(doc["id"], item.get("page"), item.get("snippet"))
            if chunk_id is not None:
                item["chunk_id"] = chunk_id
                item["resolved_by"] = "chunk"
                item["traceable"] = True
            else:
                item["resolved_by"] = "doc"
                item["traceable"] = False
                result.warnings.append(
                    f"{draft.name}：文档「{doc['filename']}」已找到，但没定位到具体切片，"
                    "建议补充页码或原文摘录"
                )
            items.append(item)
        resolved_drafts.append(replace(draft, evidence=items))
    result.drafts = resolved_drafts
    return result


async def resolve_evidence(
    session: Any, result: PriorBuildResult, *, kb_id: int = 0
) -> PriorBuildResult:
    """数据库版证据溯源（导入脚本和接口都用它）。"""
    return await resolve_evidence_with(result, DbEvidenceLookup(session), kb_id=kb_id)
