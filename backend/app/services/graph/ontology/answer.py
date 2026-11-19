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

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document

#: 每段原文最多带多少字进提示词。太长会把骨架挤掉，太短会丢掉关键条件。
MAX_EXCERPT_CHARS = 1200
#: 一次最多带多少段原文（防止 20 步链路把上下文撑爆）
MAX_EXCERPTS = 24


@dataclass
class AnswerStep:
    phase: str
    phase_label: str
    sentence: str
    props: dict[str, Any] = field(default_factory=dict)
    excerpts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_label": self.phase_label,
            "sentence": self.sentence,
            "props": self.props,
            "excerpts": self.excerpts,
        }


@dataclass
class AnswerPlan:
    question: str
    subject: str | None = None
    conditions: list[str] = field(default_factory=list)
    steps: list[AnswerStep] = field(default_factory=list)
    #: 没有原文的环节：必须明说，不能让模型补
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "subject": self.subject,
            "conditions": self.conditions,
            "steps": [s.to_dict() for s in self.steps],
            "gaps": self.gaps,
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
    details: dict[str, dict[str, str]] = {}
    kept: list[dict[str, Any]] = []
    for step in raw_steps:
        subject = str(step.get("subject") or "")
        phase = str(step.get("phase") or "")
        is_detail = subject in primary_objects and phase in ("actor", "basis")
        if not is_detail:
            kept.append(step)
            continue
        slot = details.setdefault(subject, {})
        slot.setdefault("执行" if phase == "actor" else "依据", str(step.get("object") or ""))

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
        steps.append(
            AnswerStep(
                phase=str(step.get("phase") or ""),
                phase_label=str(step.get("phase_label") or ""),
                sentence=(
                    f"{step.get('subject')} —{step.get('predicate_label')}→ "
                    f"{step.get('object')}"
                ),
                props=props,
                excerpts=[{"chunk_id": cid} for cid in dict.fromkeys(ids)],
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
                    "text": hit["text"][:MAX_EXCERPT_CHARS],
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
    )


def render_plan(plan: AnswerPlan) -> str:
    """把骨架渲染成给模型看的段落。只有事实与原文，没有指令。"""
    lines: list[str] = []
    if plan.subject:
        lines.append(f"回答对象：{plan.subject}")
    if plan.conditions:
        lines.append("适用工况：" + "、".join(plan.conditions))
    lines.append("")
    for index, step in enumerate(plan.steps, start=1):
        head = f"{index}. [{step.phase_label}] {step.sentence}"
        props = "，".join(f"{k}：{v}" for k, v in step.props.items() if v not in (None, ""))
        if props:
            head += f"（{props}）"
        lines.append(head)
        if step.excerpts:
            for item in step.excerpts:
                where = _fmt_where(item)
                lines.append(f"   〔原文｜{where}〕{item['text']}")
        else:
            lines.append("   〔原文｜未提供〕")
    if plan.gaps:
        lines.append("")
        lines.append("以下环节原文未提供，必须在回答里如实说明：" + "；".join(plan.gaps))
    return "\n".join(lines)


SYSTEM_PROMPT = """你是水利工程领域的答题助手。用户的问题已经由本体模型拆成一条"处理链路"，
下面给你的是**固定的回答骨架**和**每一步对应的原文**。

必须遵守：
1. 严格按骨架的顺序写，一个环节一段，不要调整先后、不要合并不同环节；
2. 只能使用〔原文〕里出现的信息。骨架和原文里都没有的数字、名称、时间、单位，一律不许出现；
3. 每段末尾用【文档名 p.页码】标注该段依据：文档名和页码照抄该段〔原文｜…〕里
   给出的那串（例如【高洞子水库防洪预案.pdf p.51】），不要自己改写成"文件"或编造；
4. 标着〔原文｜未提供〕的环节，用一句话如实说明"原文未提供该环节的依据"，不要猜测补全；
5. 不要出现"本体""知识图谱""事实包""骨架"这类系统内部术语，直接讲业务本身；
6. 不要在正文开头复述用户的问题，也不要写"根据您的问题"之类的话。"""


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
