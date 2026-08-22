"""IR validator（P1-1）：检查 DocumentElement 列表是否合法。

校验规则：
- page_start ≤ page_end（都有值时）；页码 ≥ 1
- bbox 为 4 元组且非负（x0<=x1, y0<=y1）
- reading_order 严格递增（按列表顺序即序；元素自带值必须与顺序一致）
- parent_id 引用存在（非 None 时）
- heading_level ∈ 1..6（非 None 时）
- table 行列一致（rows 非空时每行长度 = header_path 长度）
- text 非空（除非明确标记 empty）
- element_id 唯一
"""
from __future__ import annotations

from app.services.parser.ir import DocumentElement, ElementType


def validate_elements(elements: list[DocumentElement]) -> list[str]:
    """返回错误列表（空 = 全部合法）。"""
    errors: list[str] = []
    ids: set[str] = set()

    for i, el in enumerate(elements):
        # element_id 唯一
        if el.element_id in ids:
            errors.append(f"[{el.element_id}] element_id 重复")
        ids.add(el.element_id)

        # text 非空（HEADER/FOOTER 可空？不——空文本无意义）
        if not el.text.strip() and el.type not in (ElementType.FIGURE,):
            errors.append(f"[{el.element_id}] text 为空")

        # 页码范围
        if el.page_start is not None and el.page_start < 1:
            errors.append(f"[{el.element_id}] page_start < 1: {el.page_start}")
        if el.page_end is not None and el.page_end < 1:
            errors.append(f"[{el.element_id}] page_end < 1: {el.page_end}")
        if (
            el.page_start is not None
            and el.page_end is not None
            and el.page_start > el.page_end
        ):
            errors.append(
                f"[{el.element_id}] page_start={el.page_start} > page_end={el.page_end}"
            )

        # bbox
        if el.bbox is not None:
            if len(el.bbox) != 4:
                errors.append(f"[{el.element_id}] bbox 不是 4 元组: {el.bbox}")
            else:
                x0, y0, x1, y1 = el.bbox
                if x0 < 0 or y0 < 0 or x1 < x0 or y1 < y0:
                    errors.append(f"[{el.element_id}] bbox 非法: {el.bbox}")

        # reading_order 与顺序一致
        if el.reading_order != i:
            errors.append(
                f"[{el.element_id}] reading_order={el.reading_order} 与位置 {i} 不一致"
            )

        # heading_level 范围
        if el.heading_level is not None and not (1 <= el.heading_level <= 6):
            errors.append(f"[{el.element_id}] heading_level 越界: {el.heading_level}")

        # table 行列一致
        if el.table is not None:
            rows = el.table.get("rows")
            header = el.table.get("header_path") or []
            if rows and header:
                for ri, row in enumerate(rows):
                    if len(row) != len(header):
                        errors.append(
                            f"[{el.element_id}] table 行 {ri} 列数 {len(row)} != 表头 {len(header)}"
                        )

    # parent_id 引用存在（第二遍，此时 ids 已全）
    for el in elements:
        if el.parent_id is not None and el.parent_id not in ids:
            errors.append(f"[{el.element_id}] parent_id 悬空: {el.parent_id}")

    return errors
