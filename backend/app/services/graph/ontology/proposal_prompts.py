"""F5 图提案的**提示词侧**：对模型说什么、怎么把它的回答抠出来。

和 `proposals.py`（校验侧：什么能收）分开，是因为这两件事改动的理由完全不同——
提示词是"怎么问更准"，校验是"什么算数"。混在一个文件里，调提示词时容易顺手
放松一道校验。

`tbox_digest` 还有第二个用途：**它是对本体定义质量的一次体检**。模型提不出东西，
常常是因为类型的字段说明写得太含糊（比如 `note: 备注`），它不知道该往哪看。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.services.graph.ontology.anchoring import Candidate
from app.services.graph.ontology.registry import AttributeDef, OntologyRegistry

if TYPE_CHECKING:  # 只为类型标注，避免和 proposals.py 循环导入
    from app.services.graph.ontology.proposals import Known

_MAX_CHUNK_CHARS = 1200    # 每段送进模型的字数上限
_MAX_CATALOG = 120         # 最多列这么多个已有实例名

_TYPE_LABEL = {
    "number": "数字",
    "enum": "枚举",
    "string": "文本",
    "text": "文本",
    "boolean": "是/否",
    "list": "列表",
    "range": "区间[下限,上限]",
}


def _attr_line(attr: AttributeDef) -> str:
    label = _TYPE_LABEL.get(attr.type, attr.type)
    if attr.type == "enum" and attr.values:
        label = f"枚举: {'/'.join(attr.values)}"
    text = f"{attr.name}（{label}）"
    if attr.description:
        text += f" {attr.description}"
    return text


def tbox_digest(registry: OntologyRegistry) -> str:
    """把 TBox 压成模型能读的一段：类型 → 中文名 → 字段；关系 → 允许的端点。"""
    lines = ["【实体类型】只能从下面选，类型标识照抄："]
    for ent in registry.entity_types.values():
        attrs = "；".join(_attr_line(a) for a in ent.attributes.values()) or "（没定义字段）"
        lines.append(f"- {ent.name}（{ent.display_name}）：{attrs}")
    lines.append("")
    lines.append("【关系】只能从下面选，两端的类型必须照写：")
    for rel in registry.relation_types.values():
        sources = "/".join(rel.all_sources())
        targets = "/".join(rel.target_types or (rel.target,))
        label = rel.display_name or rel.description or rel.name
        lines.append(f"- {rel.name}（{label}）：{sources} → {targets}")
    return "\n".join(lines)


def catalog_section(by_uid: dict[str, "Known"]) -> str:
    """把库里已有的实例名喂给模型——不复用就会造出第二个「Ⅱ级响应」。"""
    if not by_uid:
        return "【库里已有的实例】暂无。"
    items = list(by_uid.values())
    shown = items[:_MAX_CATALOG]
    lines = ["【库里已有的实例】关系能连到它们的，直接用名字，不要重复新建："]
    lines.extend(f"- {k.name}（{k.entity_type}）" for k in shown)
    if len(items) > len(shown):
        lines.append(f"- …… 另有 {len(items) - len(shown)} 个未列出")
    return "\n".join(lines)


def corpus_section(chunks: list[Candidate]) -> str:
    return "\n\n".join(
        f"[片段{i + 1}]（{c.doc_name} 第{c.printed_page or c.page or '?'}页）\n"
        f"{c.content[:_MAX_CHUNK_CHARS]}"
        for i, c in enumerate(chunks)
    )


SYSTEM_PROMPT = (
    "你是水利工程资料的本体抽取员。任务：从给定原文片段里找出**本体实例**和它们"
    "之间的**关系**。只依据给出的片段，严禁推测、严禁用常识补全、严禁编造。"
    "输出严格 JSON，不要解释。"
)

_OUTPUT_SHAPE = (
    '{"instances":[{"type":"实体类型","name":"实例名","props":{},"chunk":片段序号,'
    '"quote":"逐字原文"}],'
    '"relations":[{"subject":"实例名或uid","predicate":"关系标识","object":"实例名或uid",'
    '"chunk":片段序号,"quote":"逐字原文"}]}'
)

_RULES = (
    "规则（每条都硬性执行）：\n"
    "1. quote 必须是片段里**连续出现的原文**，逐字抄，不许改写、不许把两处拼在一起；\n"
    "2. 只提片段里明确写到的内容，原文没写的宁可少提；\n"
    "3. 类型标识、关系标识只能从上面两张表里选，照抄；\n"
    "4. 关系两端连到库里已有实例的，用**它的名字**引用；连到本次新提实例的，"
    "必须在 instances 里同时声明它，否则这条关系作废；\n"
    "5. 库里已有的实例不要重复新建（如「Ⅱ级响应」）；\n"
    "6. props 里只填原文明确写到的字段，值必须在 quote 里出现。"
)

_LINK_RULES = (
    "要求（每条都硬性执行）：\n"
    "1. **每条关系至少有一端是上面那个实例**，不要扯到与它无关的东西；\n"
    "2. 另一端如果库里已有，用**它的名字**引用；原文里明确写了但库里没有的，"
    "必须在 instances 里同时声明它，否则这条关系作废；\n"
    "3. 端点方向按关系表写（谁是主语谁是宾语），**写反了整条作废**；\n"
    "4. quote 必须是片段里**连续出现的原文**，逐字抄；\n"
    "5. 关系标识只能从上面那张表里选，照抄。"
)


def build_scan_prompt(
    registry: OntologyRegistry, chunks: list[Candidate], by_uid: dict[str, "Known"]
) -> str:
    """按文档整体扫（C-2）。"""
    return (
        f"{tbox_digest(registry)}\n\n"
        f"{catalog_section(by_uid)}\n\n"
        f"【原文片段】\n{corpus_section(chunks)}\n\n"
        f"请输出 JSON：\n{_OUTPUT_SHAPE}\n\n"
        f"{_RULES}"
    )


def build_link_prompt(
    registry: OntologyRegistry,
    target: "Known",
    chunks: list[Candidate],
    by_uid: dict[str, "Known"],
) -> str:
    """补边（C-3）：专门问「这个实例还跟谁有关系」。

    和整体扫的区别不在格式而在**焦点**：整体扫是漫无目的地读，容易把同一份
    原文里所有实体一股脑提出来；补边只围绕一个实例问，命中率更高、也更容易
    被人在界面上逐条判断。
    """
    return (
        f"{tbox_digest(registry)}\n\n"
        f"{catalog_section(by_uid)}\n\n"
        f"【要补边的实例】{target.name}（{target.entity_type}）\n\n"
        f"【原文片段】\n{corpus_section(chunks)}\n\n"
        f"请找出这个实例与其他实例之间的关系，输出 JSON：\n{_OUTPUT_SHAPE}\n\n"
        f"{_LINK_RULES}"
    )


def parse_payload(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从模型输出里抠出 instances / relations（可能包 ```json 或带解释）。"""
    raw = (text or "").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return [], []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return [], []
    if not isinstance(data, dict):
        return [], []
    insts = [i for i in (data.get("instances") or []) if isinstance(i, dict)]
    rels = [r for r in (data.get("relations") or []) if isinstance(r, dict)]
    return insts, rels
