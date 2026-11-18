"""O8-1 流程链条导入：一张 Excel 把「阶段 → 动作 → 工具 / 数据」建成实例和边。

**为什么要导入而不是一条条点**：一条完整流水线是 5 个阶段、每个阶段几个动作，
再加工具和数据，三四十次点击；而且最容易错的是关系端点（NEXT_STAGE 连反了、
动作挂错阶段）。表格里一行就是一条完整动作，程序按行推导出四类边，
人只需要看「这张表对不对」。

**只建 TBox 里声明过的类型和关系**：库里没有 Stage/Action/Tool/DataAsset
或没有 NEXT_STAGE 等关系时直接报错，不偷偷造类型——流程长什么样是人定的。

列（按表头识别，顺序无关）：

    阶段 | 阶段序号 | 动作 | 工具 | 工具来源 | 需要的数据 | 数据格式 | 说明

- 阶段留空 → 沿用上一行（一个阶段写好几行动作时不用重复填）
- 阶段序号不填 → 按阶段首次出现的顺序 1、2、3…
- 工具 / 需要的数据留空 → 这一行只建动作，不建对应边
- 认不出的表头会列出来，不猜
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, BinaryIO

from app.services.graph.ontology.instances import InstanceDraft
from app.services.graph.ontology.relations import RelationDraft
from app.services.graph.ontology.registry import OntologyRegistry
from app.services.graph.ontology.validator import validate_relation

# 表头 → 字段（多种写法都认）
_HEADERS: dict[str, str] = {
    "阶段": "stage",
    "阶段名称": "stage",
    "阶段序号": "stage_order",
    "动作": "action",
    "动作名称": "action",
    "步骤": "action",
    "工具": "tool",
    "工具名称": "tool",
    "工具来源": "tool_kind",
    "工具类型": "tool_kind",
    "需要的数据": "data_asset",
    "数据": "data_asset",
    "数据资产": "data_asset",
    "数据格式": "data_format",
    "格式": "data_format",
    "说明": "note",
    "备注": "note",
}

# 这套导入依赖的类型和关系（缺任何一个都直接报错，而不是悄悄建一半）
_ENTITY_TYPES = ("Stage", "Action", "Tool", "DataAsset")
_RELATION_TYPES = ("NEXT_STAGE", "PERFORMED_BY", "USES_TOOL", "REQUIRES_DATA")

_MAX_ROWS = 500


@dataclass
class FlowRow:
    row_no: int
    stage: str
    action: str
    tool: str = ""
    tool_kind: str = ""
    data_asset: str = ""
    data_format: str = ""
    note: str = ""


@dataclass
class FlowPlan:
    """一张表的解析结果：待建实例 + 待建关系（都还没落库）。"""

    rows: list[FlowRow] = field(default_factory=list)
    stages: dict[str, int] = field(default_factory=dict)  # 阶段名 → 序号
    instances: list[InstanceDraft] = field(default_factory=list)
    relations: list[RelationDraft] = field(default_factory=list)
    mapped: dict[str, str] = field(default_factory=dict)
    skipped_headers: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "rows": len(self.rows),
            "stages": len(self.stages),
            "actions": sum(1 for i in self.instances if i.entity_type == "Action"),
            "tools": sum(1 for i in self.instances if i.entity_type == "Tool"),
            "data_assets": sum(1 for i in self.instances if i.entity_type == "DataAsset"),
            "relations": len(self.relations),
        }

    def to_dict(self, *, limit: int = 100) -> dict[str, Any]:
        return {
            **self.counts,
            "mapped": self.mapped,
            "skipped_headers": self.skipped_headers,
            "errors": self.errors,
            "stage_order": self.stages,
            # 注意别叫 rows：counts 里的 rows 是行数，同名会把数字覆盖成列表
            "preview": [
                {
                    "row_no": r.row_no,
                    "stage": r.stage,
                    "action": r.action,
                    "tool": r.tool,
                    "data_asset": r.data_asset,
                }
                for r in self.rows[:limit]
            ],
        }


def _require_types(reg: OntologyRegistry) -> None:
    missing = [t for t in _ENTITY_TYPES if reg.entity(t) is None]
    if missing:
        raise ValueError(
            f"当前图谱库「{reg.library_name}」缺少流程类型：{'、'.join(missing)}；"
            "请切到「流程骨架本体」这类定义了流程类型的库"
        )
    missing_rel = [r for r in _RELATION_TYPES if reg.relation(r) is None]
    if missing_rel:
        raise ValueError(
            f"当前图谱库「{reg.library_name}」缺少流程关系：{'、'.join(missing_rel)}"
        )


def _cell(row: tuple[Any, ...], header_map: dict[str, int], header: str) -> str:
    idx = header_map.get(header)
    if idx is None or idx >= len(row):
        return ""
    value = row[idx]
    return "" if value is None else str(value).strip()


def build_plan(stream: BinaryIO, reg: OntologyRegistry) -> FlowPlan:
    """读表 → 推导出要建的实例和关系（不碰数据库）。"""
    from openpyxl import load_workbook

    _require_types(reg)

    wb = load_workbook(stream, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise ValueError("表是空的") from None
        headers = [str(c).strip() if c is not None else "" for c in header_row]

        header_map: dict[str, int] = {}
        plan = FlowPlan()
        for idx, header in enumerate(headers):
            if not header:
                continue
            target = _HEADERS.get(header)
            if target is None:
                plan.skipped_headers.append(header)
                continue
            header_map[target] = idx
            plan.mapped[header] = target
        if "stage" not in header_map or "action" not in header_map:
            raise ValueError("表头里需要「阶段」和「动作」两列")

        last_stage = ""
        for offset, raw in enumerate(rows_iter, start=2):
            if len(plan.rows) >= _MAX_ROWS:
                break
            if raw is None or all(c is None or str(c).strip() == "" for c in raw):
                continue
            stage = _cell(raw, header_map, "stage") or last_stage
            action = _cell(raw, header_map, "action")
            if not action:
                plan.errors.append({"row_no": offset, "message": "动作为空（阶段行请填动作）"})
                continue
            if not stage:
                plan.errors.append({"row_no": offset, "message": "第一行必须有阶段名"})
                continue
            last_stage = stage
            order_text = _cell(raw, header_map, "stage_order")
            if stage not in plan.stages:
                plan.stages[stage] = _parse_order(order_text, len(plan.stages) + 1)
            plan.rows.append(
                FlowRow(
                    row_no=offset,
                    stage=stage,
                    action=action,
                    tool=_cell(raw, header_map, "tool"),
                    tool_kind=_cell(raw, header_map, "tool_kind"),
                    data_asset=_cell(raw, header_map, "data_asset"),
                    data_format=_cell(raw, header_map, "data_format"),
                    note=_cell(raw, header_map, "note"),
                )
            )
    finally:
        wb.close()

    _build_drafts(plan, reg)
    return plan


def _parse_order(text: str, default: int) -> int:
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _build_drafts(plan: FlowPlan, reg: OntologyRegistry) -> None:
    """表 → 实例草稿 + 关系草稿。关系先校验端点，不合法的记错误不硬写。"""
    seen: set[tuple[str, str]] = set()  # (类型, 名称) 去重：同名工具跨阶段只建一次

    def add_instance(entity_type: str, name: str, props: dict[str, Any]) -> str:
        uid = reg.uid_for(entity_type, name)
        key = (entity_type, name)
        if key not in seen:
            seen.add(key)
            plan.instances.append(
                InstanceDraft(
                    profile_id=reg.profile_id,
                    entity_type=entity_type,
                    name=name,
                    uid=uid,
                    props={k: v for k, v in props.items() if v not in (None, "")},
                    source="import",
                )
            )
        return uid

    stage_uids: dict[str, str] = {}
    for stage, order in plan.stages.items():
        stage_uids[stage] = add_instance("Stage", stage, {"order": order})

    # 阶段链：按序号排，相邻两阶段连 NEXT_STAGE
    ordered = sorted(plan.stages.items(), key=lambda kv: (kv[1], kv[0]))
    for (prev_name, _), (next_name, _) in zip(ordered, ordered[1:]):
        _add_relation(
            plan, reg, "NEXT_STAGE", "Stage", stage_uids[prev_name], "Stage",
            stage_uids[next_name], f"{prev_name} → {next_name}",
        )

    for row in plan.rows:
        action_uid = add_instance(
            "Action", row.action, {"tool_hint": row.tool, "note": row.note}
        )
        _add_relation(
            plan, reg, "PERFORMED_BY", "Stage", stage_uids[row.stage], "Action",
            action_uid, f"{row.stage} → {row.action}",
        )
        if row.tool:
            tool_uid = add_instance(
                "Tool", row.tool, {"kind": _tool_kind(row.tool_kind), "note": row.note}
            )
            _add_relation(
                plan, reg, "USES_TOOL", "Action", action_uid, "Tool", tool_uid,
                f"{row.action} → {row.tool}",
            )
        if row.data_asset:
            data_uid = add_instance(
                "DataAsset", row.data_asset, {"format": row.data_format, "note": row.note}
            )
            _add_relation(
                plan, reg, "REQUIRES_DATA", "Action", action_uid, "DataAsset", data_uid,
                f"{row.action} → {row.data_asset}",
            )


_TOOL_KINDS = {
    "内部": "internal", "内置": "internal", "internal": "internal",
    "外部": "external", "external": "external",
    "mcp": "mcp", "MCP": "mcp",
}


def _tool_kind(text: str) -> str:
    """工具来源统一成枚举值；认不出就留空（宁可空着，也不写一个不合法的枚举）。"""
    return _TOOL_KINDS.get((text or "").strip(), "")


def _add_relation(
    plan: FlowPlan,
    reg: OntologyRegistry,
    predicate: str,
    subject_type: str,
    subject_uid: str,
    object_type: str,
    object_uid: str,
    label: str,
) -> None:
    """校验端点后加入计划；不合法就记错误（不写一条错的边进去）。"""
    check = validate_relation(reg, predicate, subject_type, object_type)
    if not check.ok:
        plan.errors.append({"message": f"{label}：{'；'.join(check.errors)}"})
        return
    plan.relations.append(
        RelationDraft(
            profile_id=reg.profile_id,
            subject_uid=subject_uid,
            predicate=predicate,
            object_uid=object_uid,
            source="import",
        )
    )
