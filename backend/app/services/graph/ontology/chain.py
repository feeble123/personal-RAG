"""把多跳事实排成一条"能读懂"的链路骨架。

分工（C 方案）：**这里只做结构化，不润色**。哪些事实、按什么顺序、附哪条证据，
全部由程序决定；模型以后只负责把这些骨架句子说得通顺（且不许添事实）。

为什么不让模型自己排：链路的顺序就是处置顺序，排错了比措辞差严重得多。
程序排序是确定的、可测的，出问题能一眼看出是哪条关系被放错了位置。

分档（phase）按"人讲一件事的顺序"：
    起点 → 工况/依据 → 流程推进 → 谁来做 → 用什么/要什么数据 → 影响谁 → 怎么应对

关系名对不上任何一档的，落在"其他"里——宁可多给一条，也不要悄悄丢掉。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: 关系 → 档位。键统一大写比较，兼容小写的历史边名（downstream_of 之类）。
_PHASE_OF: dict[str, str] = {
    # 依据 / 工况
    "STIPULATES": "basis",
    "APPLIES_TO": "basis",
    "REQUIRES": "basis",
    "CONSTRAINED_BY": "basis",
    # 流程推进
    "NEXT_STAGE": "flow",
    "PRECEDES": "flow",
    "FOLLOWED_BY": "flow",
    # 谁来做
    "PERFORMED_BY": "actor",
    "OWNED_BY": "actor",
    "MANAGED_BY": "actor",
    "RESPONSIBLE_FOR": "actor",
    # 用什么 / 要什么
    "USES_TOOL": "means",
    "REQUIRES_DATA": "means",
    "PRODUCES": "means",
    # 影响
    "AFFECTS": "impact",
    "DOWNSTREAM_OF": "impact",
    "TRIGGERS": "impact",
    "IMPACTS": "impact",
    # 应对
    "MITIGATED_BY": "response",
    "RESPONDS_WITH": "response",
    "RESPONSE_TO": "response",
    "PROTECTS": "response",
}

#: 输出顺序（也是给人读的顺序）
PHASE_ORDER = ["subject", "basis", "flow", "actor", "means", "impact", "response", "other"]

PHASE_LABEL = {
    "subject": "起点",
    "basis": "工况与依据",
    "flow": "流程推进",
    "actor": "谁处置",
    "means": "用什么",
    "impact": "影响谁",
    "response": "怎么应对",
    "other": "其他关系",
}


@dataclass
class ChainStep:
    phase: str
    hop: int
    subject: str
    predicate: str
    predicate_label: str
    object: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    condition_matched: bool = False
    #: 宾语节点自己的属性（控泄流量、起调水位…）。
    #: 只报名字的话，「10~20年一遇控泄方案」看不出到底泄多少——而这恰恰是
    #: 用户最想知道的数。属性是节点事实里本来就有，顺手带上来，不需要额外查询。
    object_props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_label": PHASE_LABEL.get(self.phase, self.phase),
            "hop": self.hop,
            "subject": self.subject,
            "predicate": self.predicate,
            "predicate_label": self.predicate_label,
            "object": self.object,
            "object_props": self.object_props,
            "evidence": self.evidence,
            "condition_matched": self.condition_matched,
        }

    def describe(self) -> str:
        """一句骨架：`青溪水库 —规定→ 泄洪方案A（规程 p.8）`。

        出处要挑**能落到文档页码**的那一条：证据列表里可能先放的是边上的原文摘要
        （只有 text），拿它拼出来就是一对空括号——用户会以为这条没出处。
        """
        where = ""
        located = next(
            (e for e in self.evidence if e.get("doc_name") or e.get("page") or e.get("printed_page")),
            None,
        )
        if located is not None:
            bits = [str(located.get("doc_name") or "")]
            page = located.get("printed_page") or located.get("page")
            if page:
                bits.append(f"p.{page}")
            where = "（" + " ".join(b for b in bits if b) + "）"
        return f"{self.subject} —{self.predicate_label}→ {self.object}{where}"


@dataclass
class Chain:
    subject: str | None = None
    conditions: list[str] = field(default_factory=list)
    steps: list[ChainStep] = field(default_factory=list)
    #: 没有证据的环节：链路上这些点无法核对，必须明说
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "conditions": self.conditions,
            "steps": [s.to_dict() for s in self.steps],
            "gaps": self.gaps,
            "outline": self.outline(),
        }

    def outline(self) -> list[str]:
        """按档位分组的骨架文本，给前端直接渲染、也给后续措辞当底稿。"""
        lines: list[str] = []
        if self.subject:
            lines.append(f"【起点】{self.subject}")
        if self.conditions:
            lines.append("【工况】" + "、".join(self.conditions))
        for phase in PHASE_ORDER:
            if phase == "subject":
                continue
            steps = [s for s in self.steps if s.phase == phase]
            if not steps:
                continue
            lines.append(f"【{PHASE_LABEL[phase]}】")
            lines.extend(f"  {s.describe()}" for s in steps)
        if self.gaps:
            lines.append("【缺证据的环节】")
            lines.extend(f"  {g}" for g in self.gaps)
        return lines


#: 证据补全函数：chunk_ids → [{chunk_id, doc_name, page, printed_page, snippet?}]
EvidenceLookup = Callable[[Iterable[int]], "list[dict[str, Any]]"]


def phase_of(predicate: str) -> str:
    return _PHASE_OF.get((predicate or "").upper(), "other")


def resolve_phase(predicate: str, declared: str | None = None) -> str:
    """关系名 → 链路档位，TBox 里声明的优先。

    为什么需要这条：内置表只认识历史那 22 个关系名。新领域（比如水库防汛的
    STIPULATES / TRIGGERS / NEXT_STEP）如果没人告诉引擎它属于哪一档，整条链路
    会掉进「其他关系」——顺序就乱了，等于编排失效。
    YAML 里写 `phase:` 就能解决，引擎不认识任何具体领域。
    """
    if declared and declared in PHASE_ORDER:
        return declared
    return phase_of(predicate)


def _evidence_of(edge: dict[str, Any], lookup: EvidenceLookup | None) -> list[dict[str, Any]]:
    """一条边的证据：优先用边自带的说明，再把 chunk 换成文档与页码。"""
    chunk_ids = [int(c) for c in (edge.get("chunk_ids") or []) if str(c).isdigit()]
    resolved = lookup(chunk_ids) if lookup and chunk_ids else []
    evidence: list[dict[str, Any]] = []
    if edge.get("evidence_text"):
        evidence.append({"text": edge["evidence_text"], "chunk_ids": chunk_ids})
    for item in resolved:
        evidence.append(item)
    if not evidence and chunk_ids:
        evidence.append({"text": "", "chunk_ids": chunk_ids})
    return evidence


def build_chain(
    facts: list[dict[str, Any]],
    *,
    conditions: list[str] | None = None,
    evidence_lookup: EvidenceLookup | None = None,
) -> Chain:
    """把事实排成链路。

    顺序规则：档位优先（人讲的顺序）→ 跳数（近的在前）→ 原始顺序（BFS 发现顺序）。
    这样"下一步做什么"不会排到"用什么工具"后面，长链也不会乱。
    """
    nodes = [f for f in facts if f.get("kind") == "graph_node"]
    edges = [f for f in facts if f.get("kind") == "graph_edge"]
    # 节点名 → 属性：链路里每一步把宾语的属性带上（控泄流量这类数就在里面）
    props_by_name = {
        str(n.get("name") or n.get("subject") or ""): dict(n.get("props") or {})
        for n in nodes
    }

    subject = None
    for node in nodes:
        if node.get("condition_matched"):
            subject = node.get("name") or node.get("subject")
            break
    if subject is None and nodes:
        subject = nodes[0].get("name") or nodes[0].get("subject")
    if subject is None and edges:
        # 只有边没有节点事实时，用第一条边的主语当起点（总得有个开头）
        subject = edges[0].get("subject") or None

    steps: list[ChainStep] = []
    for order, edge in enumerate(edges):
        step = ChainStep(
            phase=resolve_phase(
                str(edge.get("predicate") or ""), edge.get("phase")
            ),
            hop=int(edge.get("hop") or 0),
            subject=str(edge.get("subject") or ""),
            predicate=str(edge.get("predicate") or ""),
            predicate_label=str(edge.get("predicate_label") or edge.get("predicate") or ""),
            object=str(edge.get("object") or ""),
            evidence=_evidence_of(edge, evidence_lookup),
            condition_matched=bool(edge.get("condition_matched")),
            object_props=dict(
                edge.get("object_props")
                or props_by_name.get(str(edge.get("object") or ""), {})
            ),
        )
        steps.append(step)
        # 稳定排序用的原始序号挂在这里（不进 to_dict）
        step.__dict__["_order"] = order

    steps.sort(key=lambda s: (PHASE_ORDER.index(s.phase), s.hop, s.__dict__.get("_order", 0)))

    gaps = [s.describe() for s in steps if not s.evidence]
    return Chain(subject=subject, conditions=list(conditions or []), steps=steps, gaps=gaps)
