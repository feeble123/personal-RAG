"""从 Excel 表头推断 TBox 属性（F3-3）。

**只做预览，不写任何东西。** 表格里的列名是给人看的（「安全泄量(m³/s)」），
TBox 要的是程序标识（`safe_discharge_m3s`）——这中间的转换没法完全自动，
所以这里给出建议值 + 样例值，由用户在界面上确认或改完再落库。

类型推断规则（宁可保守）：
- 前若干行全空 → string
- 全都可以解析成数字 → number
- 取值少且都很短 → enum（把去重后的值当可选值）
- 其余 → string
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_SAMPLE_ROWS = 200      # 只看前 200 行，够判断类型了
_ENUM_MAX_VALUES = 10       # 去重后不超过 10 个值才建议枚举
_ENUM_MAX_LEN = 12          # 枚举值都很短才建议枚举


@dataclass
class ColumnSuggestion:
    header: str
    name: str
    type: str = "string"
    values: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_name(header: str, index: int) -> str:
    """能当标识就直接用，不能就用 col_N（中文列名转拼音不靠谱，留给用户改）。"""
    text = (header or "").strip()
    if _SAFE_NAME_RE.match(text):
        return text
    # 去掉括号单位后如果还剩 ASCII 标识就用它，如 "safe_discharge(m3/s)"
    head = re.split(r"[（(]", text)[0].strip()
    if _SAFE_NAME_RE.match(head):
        return head
    return f"col_{index}"


def _looks_number(value: str) -> bool:
    try:
        float(str(value).replace(",", "").strip())
        return True
    except (TypeError, ValueError):
        return False


def _infer_type(values: list[str]) -> tuple[str, list[str]]:
    present = [v for v in values if v.strip()]
    if not present:
        return "string", []
    if all(_looks_number(v) for v in present):
        return "number", []
    distinct = []
    for v in present:
        if v not in distinct:
            distinct.append(v)
    if len(distinct) <= _ENUM_MAX_VALUES and all(len(v) <= _ENUM_MAX_LEN for v in distinct):
        return "enum", distinct
    return "string", []


def suggest_columns(stream: Any, *, sheet: str | None = None) -> list[ColumnSuggestion]:
    """读第一个工作表（或指定 sheet）的表头 + 前若干行，给出属性建议。"""
    from openpyxl import load_workbook  # 延迟导入

    wb = load_workbook(stream, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            return []

        headers = [str(c).strip() if c is not None else "" for c in header_row]
        columns: list[list[str]] = [[] for _ in headers]
        for i, row in enumerate(rows):
            if i >= _MAX_SAMPLE_ROWS:
                break
            for idx in range(len(headers)):
                cell = row[idx] if idx < len(row) else None
                if cell is not None and str(cell).strip():
                    columns[idx].append(str(cell).strip())

        out: list[ColumnSuggestion] = []
        for idx, header in enumerate(headers):
            if not header:
                continue
            values = columns[idx]
            kind, enum_values = _infer_type(values)
            out.append(
                ColumnSuggestion(
                    header=header,
                    name=_safe_name(header, idx + 1),
                    type=kind,
                    values=enum_values,
                    samples=list(dict.fromkeys(values))[:3],
                    description=header if _safe_name(header, idx + 1) != header else "",
                )
            )
        return out
    finally:
        wb.close()


def parse_yaml_profile(text: str) -> dict[str, Any]:
    """校验一份待导入的 TBox YAML，返回 profile_id / display_name 等元信息。

    真正落盘由调用方做；这里只回答「这份 YAML 是不是一份能用的 TBox」。
    """
    from ruamel.yaml import YAML

    from app.services.graph.ontology.registry import ProfileError

    try:
        doc = YAML(typ="safe").load(text)
    except Exception as exc:  # noqa: BLE001 - YAML 语法错要给人看懂的话
        raise ProfileError(f"YAML 解析失败：{exc}") from exc
    if not isinstance(doc, dict):
        raise ProfileError("YAML 顶层必须是一个映射（version / profile_id / entity_types…）")
    if not doc.get("profile_id"):
        raise ProfileError("缺少 profile_id")
    if not doc.get("entity_types"):
        raise ProfileError("缺少 entity_types（至少定义一类实体）")
    return {
        "profile_id": str(doc["profile_id"]),
        "display_name": str(doc.get("display_name") or doc["profile_id"]),
        "description": str(doc.get("description") or ""),
    }
