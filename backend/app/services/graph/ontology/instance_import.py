"""实例 Excel 批量导入（F3-3 补充）：一张表 = 一批同类实例。

**列怎么对上属性**
1. 表头正好是属性标识（`design_flow`）→ 直接对上
2. 表头是属性的中文说明（`设计流量`）→ 按 description 对上
3. 都对不上 → 跳过该列并在结果里说明（**不猜**：猜错了数据就是脏的）

**名字列**：`name` / `名称` / `display_zh` 任一；没有就整批拒绝——
没有名字的实例在图上就是一个 "—"，等于没建。

**默认只预演**：调用方传 dry_run 时只返回"会建几条、哪些行有问题"，
不写库。批量写数据必须能先看一眼。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from app.services.graph.ontology.registry import EntityTypeDef, OntologyRegistry

logger = logging.getLogger(__name__)

_NAME_HEADERS = ("name", "名称", "display_zh", "实例名称")
_MAX_ROWS = 2000  # 一次导入的上限：再多就该分文件了，别把请求撑爆


@dataclass
class ImportRow:
    row_no: int
    name: str
    props: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ImportPreview:
    entity_type: str
    total: int = 0
    rows: list[ImportRow] = field(default_factory=list)
    mapped: dict[str, str] = field(default_factory=dict)   # 表头 → 属性名
    skipped_headers: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, limit: int = 50) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "total": self.total,
            "mapped": self.mapped,
            "skipped_headers": self.skipped_headers,
            "errors": self.errors,
            "rows": [
                {"row_no": r.row_no, "name": r.name, "props": r.props, "error": r.error}
                for r in self.rows[:limit]
            ],
        }


def _cast_cell(value: Any, attr_type: str) -> Any | None:
    """按属性类型转值；转不了就返回 None（由调用方记成该行错误，不静默丢）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if attr_type == "number":
        try:
            num = float(text.replace(",", ""))
        except ValueError:
            raise ValueError(f"不是数字：{text!r}")
        return int(num) if num.is_integer() else num
    if attr_type == "boolean":
        return text in ("是", "true", "True", "1", "Y", "y")
    return text


def _match_columns(headers: list[str], entity: EntityTypeDef) -> tuple[dict[str, str], list[str]]:
    """表头 → 属性名。返回 (映射, 认不出的表头)。"""
    by_desc = {a.description.strip(): a.name for a in entity.attributes.values() if a.description}
    mapped: dict[str, str] = {}
    skipped: list[str] = []
    for header in headers:
        h = (header or "").strip()
        if not h:
            continue
        if h in entity.attributes:
            mapped[h] = h
        elif h in by_desc:
            mapped[h] = by_desc[h]
        else:
            skipped.append(h)
    return mapped, skipped


def preview_import(
    stream: BinaryIO, reg: OntologyRegistry, entity_type: str
) -> ImportPreview:
    """读表 → 逐行转成 props（不碰数据库）。"""
    from openpyxl import load_workbook

    entity = reg.entity(entity_type)
    if entity is None:
        raise ValueError(f"实体类型 {entity_type!r} 不存在")

    wb = load_workbook(stream, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            raise ValueError("表是空的") from None
        headers = [str(c).strip() if c is not None else "" for c in header_row]

        name_idx = next(
            (i for i, h in enumerate(headers) if h in _NAME_HEADERS), None
        )
        if name_idx is None:
            raise ValueError(
                f"找不到名称列：表头里需要有 {' / '.join(_NAME_HEADERS)} 之一"
            )

        mapped, skipped = _match_columns(headers, entity)
        # 名称列已经被当作实例名用了，不算「认不出的表头」
        name_header = headers[name_idx]
        skipped = [s for s in skipped if s != name_header]
        preview = ImportPreview(entity_type=entity_type, mapped=mapped, skipped_headers=skipped)

        for offset, row in enumerate(rows, start=2):  # 2 = Excel 行号（表头占 1）
            if offset - 2 >= _MAX_ROWS:
                break
            if row is None or all(c is None or str(c).strip() == "" for c in row):
                continue
            preview.total += 1
            name = str(row[name_idx]).strip() if name_idx < len(row) and row[name_idx] else ""
            if not name:
                preview.errors.append({"row_no": offset, "message": "名称为空"})
                continue
            props: dict[str, Any] = {}
            error = None
            for header, attr_name in mapped.items():
                idx = headers.index(header)
                if idx >= len(row):
                    continue
                attr = entity.attributes[attr_name]
                try:
                    value = _cast_cell(row[idx], attr.type)
                except ValueError as exc:
                    error = f"列「{header}」{exc}"
                    break
                if value is not None:
                    props[attr_name] = value
            missing = [
                a.name for a in entity.attributes.values() if a.required and a.name not in props
            ]
            if error is None and missing:
                error = f"缺少必填字段：{', '.join(missing)}"
            if error:
                preview.errors.append({"row_no": offset, "name": name, "message": error})
                continue
            preview.rows.append(ImportRow(row_no=offset, name=name, props=props))
        return preview
    finally:
        wb.close()
