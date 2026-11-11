"""防洪链台账 → 图节点/关系（纯函数，可离线测试）。

台账四张表（Excel 的四个 sheet，逐行读成 dict）：

1. 工程表   工程名称 / 工程类型 / 所属水库 / 管理单位 / 所在乡镇 / 备注
2. 工况表   工况名 / 频率 / 库水位(m) / 下泄流量(m³/s) / 依据条款 / 备注
3. 河段表   河段名 / 起点 / 终点 / 长度(km) / 安全泄量(m³/s) / 依据条款 / 备注
4. 影响对象表 工况 / 河段 / 乡镇 / 村 / 是否预警 / 预警阈值 / 是否转移 / 安置点 /
             转移路线 / 依据条款 / 责任人

生成的关系（只允许 schema 里定义的 10 种）：
- 建筑物 --属于--> 水库
- 水库/建筑物 --位于--> 乡镇
- 水库/建筑物 --责任--> 管理单位
- 水库 --下游--> 河段 --下游--> 乡镇
- 村 --位于--> 乡镇
- 行政区 --需预警/需转移--> 措施
- 措施 --责任--> 责任人岗位

**为什么先做台账**：鲤鱼塘资料还在收集，台账能把链骨架先定死并可验证；
文档到位后规则抽取产出的同一批事实，可以按 edge_key 与台账合并（人工优先）。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.graph.schema import (
    Edge,
    EntityType,
    ExtractionResult,
    Node,
    RelationType,
    endpoints_allowed,
    type_value,
    uid_for,
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_TRUE_WORDS = {"是", "y", "yes", "true", "1", "√", "需要", "需", "要"}


def parse_number(value: Any) -> float | None:
    """从单元格取数：支持 1230 / 1,230 / 1230 m³/s / 100年一遇；取不到返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("，", "")
    match = _NUM_RE.search(text)
    return float(match.group()) if match else None


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_WORDS


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    return str(value).strip() if value is not None else ""


class _Builder:
    """收集节点/边并按 uid 去重；非法边记入 warnings，不中断整批导入。"""

    def __init__(self, kb_id: int) -> None:
        self.kb_id = kb_id
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple, Edge] = {}
        self.warnings: list[str] = []

    def node(
        self,
        entity_type: EntityType,
        name: str,
        props: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
    ) -> Node | None:
        name = (name or "").strip()
        if not name:
            return None
        uid = uid_for(entity_type, name, self.kb_id)
        clean_props = {k: v for k, v in (props or {}).items() if v not in (None, "", [])}
        existing = self.nodes.get(uid)
        if existing is None:
            self.nodes[uid] = Node(
                uid=uid,
                entity_type=entity_type,
                name=name,
                props=clean_props,
                aliases=list(dict.fromkeys(aliases or [])),
                kb_ids=[self.kb_id],
            )
        else:
            for k, v in clean_props.items():
                existing.props.setdefault(k, v)
            for alias in aliases or []:
                if alias and alias not in existing.aliases:
                    existing.aliases.append(alias)
        return self.nodes[uid]

    def edge(
        self,
        subject: Node | None,
        predicate: RelationType,
        obj: Node | None,
        *,
        evidence_text: str = "",
        props: dict[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> None:
        if subject is None or obj is None:
            return
        if not endpoints_allowed(predicate, subject.entity_type, obj.entity_type):
            self.warnings.append(
                f"关系端点非法，已跳过：{subject.name}({type_value(subject.entity_type)}) "
                f"-{type_value(predicate)}-> {obj.name}({type_value(obj.entity_type)})"
            )
            return
        key = (subject.uid, type_value(predicate), obj.uid, None)
        existing = self.edges.get(key)
        if existing is not None:
            if evidence_text and evidence_text not in existing.evidence_text:
                existing.evidence_text = f"{existing.evidence_text}；{evidence_text}".strip("；")
            return
        self.edges[key] = Edge(
            subject_uid=subject.uid,
            predicate=predicate,
            object_uid=obj.uid,
            kb_id=self.kb_id,
            chunk_ids=[],
            props=dict(props or {}),
            extractor="manual",
            confidence=confidence,
            status="confirmed",  # 人工台账默认视为已确认
            evidence_text=evidence_text,
        )

    def result(self) -> ExtractionResult:
        return ExtractionResult(
            nodes=list(self.nodes.values()),
            edges=list(self.edges.values()),
            warnings=self.warnings,
        )


def build_flood_chain(
    kb_id: int,
    projects: list[dict],
    conditions: list[dict],
    reaches: list[dict],
    impacts: list[dict],
) -> ExtractionResult:
    """把四张台账表组装成图谱。字段名与 Excel 表头一致（见模块 docstring）。"""
    b = _Builder(kb_id)

    # ---------- 1. 工程表 ----------
    reservoirs: dict[str, Node] = {}
    all_projects: dict[str, Node] = {}
    for row in projects:
        name = _text(row, "工程名称")
        if not name:
            b.warnings.append("工程表：缺少「工程名称」，已跳过该行")
            continue
        obj_type = _text(row, "工程类型") or "reservoir"
        prop = {
            "object_type": obj_type,
            "owner_reservoir": _text(row, "所属水库") or None,
            "dam_height_m": parse_number(_text(row, "坝高(m)")),
            "capacity_wan_m3": parse_number(_text(row, "总库容(万m³)")),
        }
        node = b.node(EntityType.ENGINEERING_OBJECT, name, prop)
        if node is None:
            continue
        all_projects[name] = node
        if obj_type == "reservoir":
            reservoirs[name] = node

        owner = _text(row, "所属水库")
        if owner and owner != name:
            parent = b.node(EntityType.ENGINEERING_OBJECT, owner, {"object_type": "reservoir"})
            b.edge(node, RelationType.PART_OF, parent, evidence_text="台账·工程表")

        town = _text(row, "所在乡镇")
        if town:
            region = b.node(EntityType.ADMIN_REGION, town, {"level": "town"})
            b.edge(node, RelationType.LOCATED_IN, region, evidence_text="台账·工程表")

        unit = _text(row, "管理单位")
        if unit:
            org = b.node(EntityType.MANAGEMENT_UNIT, unit, {"unit_type": "management"})
            b.edge(node, RelationType.RESPONSIBLE, org, evidence_text="台账·工程表")

    # ---------- 2. 工况表 ----------
    condition_nodes: dict[str, Node] = {}
    for row in conditions:
        name = _text(row, "工况名")
        if not name:
            b.warnings.append("工况表：缺少「工况名」，已跳过该行")
            continue
        node = b.node(
            EntityType.CONDITION,
            name,
            {
                "condition_type": _text(row, "工况类型") or _text(row, "工况名"),
                "frequency": _text(row, "频率") or None,
                "reservoir_level_m": parse_number(_text(row, "库水位(m)")),
                "discharge_m3s": parse_number(_text(row, "下泄流量(m³/s)")),
            },
            aliases=[_text(row, "别名")] if _text(row, "别名") else None,
        )
        if node is not None:
            condition_nodes[name] = node

    # ---------- 3. 河段表 ----------
    reach_nodes: dict[str, Node] = {}
    for row in reaches:
        name = _text(row, "河段名")
        if not name:
            b.warnings.append("河段表：缺少「河段名」，已跳过该行")
            continue
        node = b.node(
            EntityType.RIVER,
            name,
            {
                "river_type": "reach",
                "start": _text(row, "起点") or None,
                "end": _text(row, "终点") or None,
                "length_km": parse_number(_text(row, "长度(km)")),
                "safe_discharge_m3s": parse_number(_text(row, "安全泄量(m³/s)")),
            },
        )
        if node is None:
            continue
        reach_nodes[name] = node

        # 河段挂到水库下游（单水库项目：取第一个水库；多水库时由「所属水库」列指定）
        owner_name = _text(row, "所属水库") or next(iter(reservoirs), "")
        if owner_name:
            owner = reservoirs.get(owner_name) or b.node(
                EntityType.ENGINEERING_OBJECT, owner_name, {"object_type": "reservoir"}
            )
            b.edge(owner, RelationType.DOWNSTREAM_OF, node, evidence_text="台账·河段表")

    # ---------- 4. 影响对象表 ----------
    for row_no, row in enumerate(impacts, start=2):  # 第 1 行是表头
        condition_name = _text(row, "工况")
        town = _text(row, "乡镇")
        if not town:
            b.warnings.append(f"影响对象表第 {row_no} 行：缺少「乡镇」，已跳过")
            continue
        if condition_name and condition_name not in condition_nodes:
            b.warnings.append(
                f"影响对象表第 {row_no} 行：工况「{condition_name}」不在工况表内（仍会建节点）"
            )
        cond = condition_nodes.get(condition_name) or (
            b.node(EntityType.CONDITION, condition_name) if condition_name else None
        )

        town_node = b.node(EntityType.ADMIN_REGION, town, {"level": "town"})
        village = _text(row, "村")
        target = town_node
        if village:
            village_node = b.node(EntityType.ADMIN_REGION, village, {"level": "village"})
            b.edge(village_node, RelationType.LOCATED_IN, town_node, evidence_text="台账·影响对象表")
            target = village_node

        reach_name = _text(row, "河段")
        reach = reach_nodes.get(reach_name)
        if reach is not None and target is not None:
            b.edge(reach, RelationType.DOWNSTREAM_OF, target, evidence_text="台账·影响对象表")
        elif reach_name and reach is None:
            b.warnings.append(f"影响对象表第 {row_no} 行：河段「{reach_name}」不在河段表内")

        clause = _text(row, "依据条款")
        evidence = f"台账·影响对象表（{clause}）" if clause else "台账·影响对象表"
        # 依据条款没有对应的规范节点时，保留在证据文本里（doc_name/clause_no 由导入器写入证据表），
        # 不硬造 StandardClause 节点，避免「凭空出现一条条款」。
        measures: list[Node] = []

        if parse_bool(row.get("是否预警")):
            threshold = _text(row, "预警阈值")
            measure = b.node(
                EntityType.MEASURE,
                f"{town}{village or ''}·预警发布",
                {"measure_type": "warning", "trigger_threshold": threshold or None},
            )
            b.edge(target, RelationType.NEEDS_WARNING, measure, evidence_text=evidence)
            if measure is not None:
                measures.append(measure)

        if parse_bool(row.get("是否转移")):
            shelter = _text(row, "安置点")
            route = _text(row, "转移路线")
            measure = b.node(
                EntityType.MEASURE,
                f"{town}{village or ''}·转移安置",
                {"measure_type": "evacuation", "shelter": shelter or None, "route": route or None},
            )
            b.edge(target, RelationType.NEEDS_EVACUATION, measure, evidence_text=evidence)
            if measure is not None:
                measures.append(measure)

        responsible = _text(row, "责任人")
        if responsible:
            role = b.node(EntityType.MANAGEMENT_UNIT, responsible, {"unit_type": "responsible_position"})
            for measure in measures:
                b.edge(measure, RelationType.RESPONSIBLE, role, evidence_text="台账·影响对象表")

    return b.result()


def summarize(result: ExtractionResult) -> dict[str, Any]:
    """给 CLI 打印用的小结。"""
    by_entity: dict[str, int] = {}
    for node in result.nodes:
        et = type_value(node.entity_type)
        by_entity[et] = by_entity.get(et, 0) + 1
    by_rel: dict[str, int] = {}
    for edge in result.edges:
        pred = type_value(edge.predicate)
        by_rel[pred] = by_rel.get(pred, 0) + 1
    return {
        "nodes": len(result.nodes),
        "edges": len(result.edges),
        "by_entity_type": by_entity,
        "by_relation_type": by_rel,
        "warnings": result.warnings,
    }


def edge_evidence_text(edge: Edge) -> str:
    """证据文本：人工台账没有原文切片，用「表名 + 条款」说明来源。"""
    return edge.evidence_text or "人工台账"
