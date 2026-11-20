"""本体规划：把链路变成「回答骨架 + 定点原文」，交给模型按骨架写答案。

**为什么需要这一层**

原来的做法是：本体查出事实 → 事实塞进提示词 → 检索照旧 top-5 → 模型自由发挥。
结果是本体只是"多给了一段参考资料"，回答的组织方式仍然是相似度检索说了算。

这里换过来：

    本体决定 **讲什么、按什么顺序、每步引用哪段原文**
    模型只负责 **把这些原文按这个顺序写成通顺的话**

取证不再靠相似度：链路每一步都带着 `chunk_id`（建实例时挂的原文出处），
我们只取这些切片，不碰全库 top-5。**引擎不认识任何领域**——骨架、属性、原文
全部来自 TBox 与实例数据。

任何一个环节拿不到原文，就如实标成"原文未提供"，而不是让模型补一段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document

#: 每段原文最多带多少字进提示词。太长会把骨架挤掉，太短会丢掉关键条件。
MAX_EXCERPT_CHARS = 1200
#: 一次最多带多少段原文（防止 20 步链路把上下文撑爆）
MAX_EXCERPTS = 24
#: 能定位到引句时，只取引句前后这么多字的窗口。
#: 一起预案的一节里往往有 6 条措施，全部来自同一个切片（实测 424~436 字）
#: 却不相关：不收窄的话，10 个环节灌进去的是同一段话，真正的差异被稀释。
QUOTE_WINDOW = 160


def _focus_text(text: str, quote: str) -> str:
    """把切片收窄到引句附近。

    MinerU 会把数字拆开（`1 3 0 m^3 / s`），所以不能直接 `str.find`——
    按字符逐个拼一个允许任意空白的正则再找。**找不到就退回原文开头，绝不猜位置**，
    宁可多给也不能给错段。
    """
    text = (text or "").strip()
    if not text:
        return ""
    for part in (quote or "").split("；"):
        part = part.strip()
        if len(part) < 6:
            continue
        pattern = r"\s*".join(re.escape(ch) for ch in part)
        match = re.search(pattern, text)
        if match is None:
            continue
        start = max(0, match.start() - QUOTE_WINDOW)
        end = min(len(text), match.end() + QUOTE_WINDOW)
        snippet = text[start:end].strip()
        return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
    return text[:MAX_EXCERPT_CHARS]


def _quotes_by_chunk(evidence: list[Any]) -> dict[int, str]:
    """链路证据里，`text` 是建库时抄的原文引句，`chunk_ids` 是它落在哪些切片。

    这两样在链路证据里是分开的两条（一条有引句没页码、一条有页码没引句），
    这里按 chunk_id 把它们合起来，渲染时才能只取引句附近那一小段。
    """
    out: dict[int, str] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        for raw in item.get("chunk_ids") or []:
            if str(raw).isdigit():
                out.setdefault(int(raw), text)
    return out


@dataclass
class AnswerStep:
    phase: str
    phase_label: str
    sentence: str
    props: dict[str, Any] = field(default_factory=dict)
    excerpts: list[dict[str, Any]] = field(default_factory=list)
    #: 这一步是 TBox 规则推出来的（库里没有这条边）。答案里要说明来源，
    #: 不能装作是原文直述——推理和摘录混在一起，是最难查的那种错。
    derived_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "phase": self.phase,
            "phase_label": self.phase_label,
            "sentence": self.sentence,
            "props": self.props,
            "excerpts": self.excerpts,
        }
        if self.derived_by:
            out["derived_by"] = self.derived_by
        return out


@dataclass
class AnswerPlan:
    question: str
    subject: str | None = None
    conditions: list[str] = field(default_factory=list)
    steps: list[AnswerStep] = field(default_factory=list)
    #: 没有原文的环节：必须明说，不能让模型补
    gaps: list[str] = field(default_factory=list)

    def sections(self) -> list[dict[str, Any]]:
        """按档位分章节（档位是 TBox 里声明的，引擎不认识具体领域）。

        为什么分章：**步骤平铺时，读者看不出哪句是判据、哪句是动作**。
        实测两段并排的「工况与依据」，一段讲调度方案一段讲等级判据，
        读起来像两份文档拼贴。分章之后顺序就是"先判据、再动作"。
        """
        groups: list[dict[str, Any]] = []
        for step in self.steps:
            if not groups or groups[-1]["phase"] != step.phase:
                groups.append(
                    {
                        "phase": step.phase,
                        "phase_label": step.phase_label,
                        "steps": [],
                    }
                )
            groups[-1]["steps"].append(step)
        return groups
    #: 同一工况下打架的结论，以及按权威级取了哪条（② 层的仲裁结果）
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "subject": self.subject,
            "conditions": self.conditions,
            "steps": [s.to_dict() for s in self.steps],
            "gaps": self.gaps,
            "conflicts": self.conflicts,
        }


def _fmt_where(item: dict[str, Any]) -> str:
    name = str(item.get("doc_name") or "").strip()
    page = item.get("printed_page") or item.get("page")
    if name and page:
        return f"{name} p.{page}"
    return name or (f"p.{page}" if page else "")


async def _load_excerpts(db: AsyncSession, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
    """按 chunk_id 取原文（定点取证：只要骨架点名的那些）。"""
    if not chunk_ids:
        return {}
    rows = (
        await db.execute(
            select(
                Chunk.id, Chunk.content, Chunk.page, Chunk.printed_page, Document.filename
            ).join(Document, Document.id == Chunk.doc_id).where(Chunk.id.in_(chunk_ids))
        )
    ).all()
    return {
        int(r[0]): {
            "chunk_id": int(r[0]),
            "text": (r[1] or "").strip(),
            "page": r[2],
            "printed_page": r[3],
            "doc_name": r[4] or "",
        }
        for r in rows
    }


async def build_answer_plan(db: AsyncSession, pack: Any) -> AnswerPlan | None:
    """从事实包里的链路生成回答骨架。没有链路时返回 None（调用方走原路径）。"""
    chain = pack.chain or {}
    raw_steps = chain.get("steps") or []
    if not raw_steps:
        return None

    # ---- 一个动作只讲一次 ----
    # 链路里有两类步骤混在一起：
    #   · 主体步骤：工况→方案、等级→启动哪个动作（回答的骨架）
    #   · 细节步骤：某个动作「由谁执行」「依据哪条」（应该是这个动作那一段的属性）
    # 不分开的话，同一件事会被写成三段（实测：传达命令、由管理处执行、依据 4.6.1）。
    # 判据只看档位，不认领域：已经被别的主体步骤当作宾语的节点，它的 actor/basis
    # 步骤就降级成该动作的附加信息。
    primary_objects = {
        str(step.get("object") or "")
        for step in raw_steps
        if str(step.get("phase") or "") in ("basis", "flow", "response", "impact")
    }
    # 哪些档位的边是"某条主干的补充信息"，应该并进那一段而不是自成一段。
    # impact 也在里面：「通知下游镇街」扇出 4 个镇，不合并就是 4 段几乎一样的话（实测）。
    detail_slot = {"actor": "执行", "basis": "依据", "impact": "影响"}
    details: dict[str, dict[str, str]] = {}
    kept: list[dict[str, Any]] = []
    for step in raw_steps:
        subject = str(step.get("subject") or "")
        phase = str(step.get("phase") or "")
        is_detail = subject in primary_objects and phase in detail_slot
        if not is_detail:
            kept.append(step)
            continue
        slot = details.setdefault(subject, {})
        label = detail_slot[phase]
        value = str(step.get("object") or "")
        if slot.get(label) and value and value not in slot[label]:
            slot[label] = f"{slot[label]}、{value}"
        else:
            slot.setdefault(label, value)

    steps: list[AnswerStep] = []
    wanted: list[int] = []
    for step in kept:
        ids = [
            int(item["chunk_id"])
            for item in (step.get("evidence") or [])
            if item.get("chunk_id") is not None
        ]
        wanted.extend(ids)
        props = dict(step.get("object_props") or {})
        props.update(details.get(str(step.get("object") or ""), {}))
        quotes = _quotes_by_chunk(list(step.get("evidence") or []))
        steps.append(
            AnswerStep(
                phase=str(step.get("phase") or ""),
                phase_label=str(step.get("phase_label") or ""),
                sentence=(
                    f"{step.get('subject')} —{step.get('predicate_label')}→ "
                    f"{step.get('object')}"
                ),
                props=props,
                excerpts=[
                    {"chunk_id": cid, "quote": quotes.get(cid, "")}
                    for cid in dict.fromkeys(ids)
                ],
                derived_by=str(step.get("derived_by") or ""),
            )
        )

    # 去重后按出现顺序取上限，避免某一步把配额吃光
    ordered_ids = list(dict.fromkeys(wanted))[:MAX_EXCERPTS]
    loaded = await _load_excerpts(db, ordered_ids)

    for step in steps:
        filled: list[dict[str, Any]] = []
        for item in step.excerpts:
            hit = loaded.get(item["chunk_id"])
            if hit is None:
                continue
            filled.append(
                {
                    "chunk_id": hit["chunk_id"],
                    "doc_name": hit["doc_name"],
                    "page": hit["page"],
                    "printed_page": hit["printed_page"],
                    "text": _focus_text(hit["text"], str(item.get("quote") or "")),
                }
            )
        step.excerpts = filled

    gaps = [s.sentence for s in steps if not s.excerpts]
    return AnswerPlan(
        question=str(getattr(pack, "question", "") or chain.get("question") or ""),
        subject=chain.get("subject"),
        conditions=list(chain.get("conditions") or []),
        steps=steps,
        gaps=gaps,
        conflicts=[c for c in (getattr(pack, "conflicts", None) or []) if isinstance(c, dict)],
    )


def render_plan(plan: AnswerPlan) -> str:
    """把骨架渲染成给模型看的段落。只有事实与原文，没有指令。"""
    lines: list[str] = []
    if plan.subject:
        lines.append(f"回答对象：{plan.subject}")
    if plan.conditions:
        lines.append("适用工况：" + "、".join(plan.conditions))
    lines.append("")
    current_phase = None
    # 同一段原文只灌一次。多个环节共用一页时（实测 10 个环节共用同一页），
    # 全量重复会把提示词撑到 59% 都是同一段话，真正的差异反而被稀释。
    # 去重键用**渲染出来的正文**而不是 chunk_id：定位到不同引句时取的是不同窗口，
    # 那是有效信息，不该被合并。
    shown_texts: set[str] = set()
    for index, step in enumerate(plan.steps, start=1):
        if step.phase != current_phase:
            current_phase = step.phase
            lines.append(f"【{step.phase_label}】")
        head = f"{index}. {step.sentence}"
        props = "，".join(f"{k}：{v}" for k, v in step.props.items() if v not in (None, ""))
        if props:
            head += f"（{props}）"
        if step.derived_by:
            head += "〔本条由本体规则推导，不是原文直接陈述〕"
        lines.append(head)
        if step.excerpts:
            for item in step.excerpts:
                where = _fmt_where(item)
                text = str(item.get("text") or "")
                if text and text in shown_texts:
                    lines.append(f"   〔原文｜{where}｜同一段原文已在上文给出〕")
                else:
                    if text:
                        shown_texts.add(text)
                    lines.append(f"   〔原文｜{where}〕{text}")
        else:
            lines.append("   〔原文｜未提供〕")
    if plan.conflicts:
        lines.append("")
        lines.append("【存在冲突，必须在回答里说明】")
        for item in plan.conflicts:
            lines.append(_conflict_line(item))
    if plan.gaps:
        lines.append("")
        lines.append("以下环节原文未提供，必须在回答里如实说明：" + "；".join(plan.gaps))
    return "\n".join(lines)


def _props_brief(props: dict[str, Any]) -> str:
    """只挑有信息量的属性写成一句话，别把整张属性表塞进提示词。"""
    bits = [f"{k}：{v}" for k, v in (props or {}).items() if k not in ("note", "依据文件的权威级（同工况冲突时取高者）")]
    return "、".join(bits)


def _conflict_line(item: dict[str, Any]) -> str:
    """一条冲突 → 一句给人看的话。"""
    subject = item.get("subject") or ""
    label = item.get("predicate_label") or item.get("predicate") or ""
    if item.get("resolved") and item.get("chosen"):
        chosen = item["chosen"]
        line = f"{subject} 的{label}：采用「{chosen.get('name')}」（{_props_brief(chosen.get('props'))}）"
        rejects = item.get("rejected") or []
        if rejects:
            parts = [
                f"「{r.get('name')}」（{_props_brief(r.get('props'))}）"
                for r in rejects
            ]
            line += "；未采用" + "、".join(parts) + "。两份文件规定的值不同，按权威级取高者。"
        return line
    names = "、".join(f"「{r.get('name')}」" for r in (item.get("rejected") or []))
    return f"{subject} 的{label}存在 {len(item.get('rejected') or [])} 条同等权威级的结论（{names}），无法自动裁定，需人工确认。"


SYSTEM_PROMPT = """你是水利工程领域的答题助手。用户的问题已经由本体模型拆成一条"处理链路"，
下面给你的是**固定的回答骨架**和**每一步对应的原文**。

必须遵守：
1. 严格按骨架的顺序写，一个环节一段，不要调整先后、不要合并不同环节；
   【】是分章标题（如【工况与依据】【怎么应对】），要写成小标题，不要把标题当成一个环节；
2. 只能使用〔原文〕里出现的信息。骨架和原文里都没有的数字、名称、时间、单位，一律不许出现；
3. 每段末尾用【文档名 p.页码】标注该段依据：文档名和页码照抄该段〔原文｜…〕里
   给出的那串（例如【高洞子水库防洪预案.pdf p.51】），不要自己改写成"文件"或编造；
4. 标着〔原文｜未提供〕的环节，用一句话如实说明"原文未提供该环节的依据"，不要猜测补全；
5. 不要出现"本体""知识图谱""事实包""骨架"这类系统内部术语，直接讲业务本身；
6. 不要在正文开头复述用户的问题，也不要写"根据您的问题"之类的话；
7. **骨架里给了【存在冲突】就必须讲**：说明采用了哪个值、另一份文件是多少、为什么没采用。
   两份文件说法不一样是事实，藏起来比说错更糟。"""


def build_plan_prompt(
    question: str,
    plan: "AnswerPlan",
    *,
    history: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """按骨架组提示词。返回 [(role, content), ...]，与 `chat.build_prompt` 同形。"""
    parts = ["【回答骨架（按此顺序）】", render_plan(plan)]
    if history:
        lines = ["【对话历史】"]
        for role, text in history:
            lines.append(f"{'用户' if role == 'user' else '助手'}：{text[:300]}")
        parts.append("\n".join(lines))
    parts.append(f"【当前问题】\n{question}")
    return [("system", SYSTEM_PROMPT), ("human", "\n\n".join(parts))]
