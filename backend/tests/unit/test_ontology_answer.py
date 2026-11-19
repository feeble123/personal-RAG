"""本体规划层（`ontology/answer.py`）：链路 → 回答骨架。

这一层决定「本体到底有没有接管回答」。两件事必须守住：

1. **一个动作只讲一次**：链路里「等级→启动动作」和「动作→由谁执行/依据哪条」
   是两条边，但读者要的是一段话。不合并的话同一件事会被写成三段。
2. **拿不到原文就如实说**：骨架里没有原文的环节必须进 gaps，
   绝不能留给模型"顺着编一段"。
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import answer as A


class _Pack:
    def __init__(self, steps, subject="20年一遇洪水", conditions=None):
        self.question = "怎么应对"
        self.chain = {
            "subject": subject,
            "conditions": conditions or ["20年一遇（P≈5.0%）"],
            "steps": steps,
            "gaps": [],
        }


def _step(subject, predicate, obj, *, phase, label, chunk_id, props=None):
    return {
        "phase": phase,
        "phase_label": {"basis": "工况与依据", "actor": "谁处置", "response": "怎么应对"}.get(
            phase, phase
        ),
        "subject": subject,
        "predicate": predicate,
        "predicate_label": label,
        "object": obj,
        "object_props": props or {},
        "evidence": [{"chunk_id": chunk_id}] if chunk_id else [],
    }


@pytest.fixture
def fake_excerpts(monkeypatch):
    async def _load(db, ids):
        return {
            cid: {
                "chunk_id": cid,
                "text": f"原文{cid}",
                "page": cid,
                "printed_page": cid,
                "doc_name": "预案.pdf",
            }
            for cid in ids
        }

    monkeypatch.setattr(A, "_load_excerpts", _load)
    return _load


class TestPlanShape:
    async def test_没有链路时返回_None(self):
        assert await A.build_answer_plan(None, _Pack([])) is None

    async def test_动作的执行者与依据并进同一段(self, fake_excerpts):
        pack = _Pack(
            [
                _step("Ⅳ级响应", "RESPONDS_WITH", "传达命令", phase="response", label="启动", chunk_id=1),
                _step("传达命令", "PERFORMED_BY", "管理处", phase="actor", label="由…执行", chunk_id=2),
                _step("传达命令", "CONSTRAINED_BY", "预案 4.6.1", phase="basis", label="依据", chunk_id=3),
            ]
        )
        plan = await A.build_answer_plan(None, pack)
        assert len(plan.steps) == 1, "执行者与依据不应各占一段"
        step = plan.steps[0]
        assert step.props.get("执行") == "管理处"
        assert step.props.get("依据") == "预案 4.6.1"

    async def test_没有原文的环节进_gaps(self, fake_excerpts):
        pack = _Pack([_step("Ⅳ级响应", "RESPONDS_WITH", "巡查", phase="response", label="启动", chunk_id=None)])
        plan = await A.build_answer_plan(None, pack)
        assert plan.gaps == ["Ⅳ级响应 —启动→ 巡查"]

    async def test_骨架里写明原文未提供(self, fake_excerpts):
        pack = _Pack([_step("Ⅳ级响应", "RESPONDS_WITH", "巡查", phase="response", label="启动", chunk_id=None)])
        plan = await A.build_answer_plan(None, pack)
        text = A.render_plan(plan)
        assert "〔原文｜未提供〕" in text
        assert "原文未提供" in text

    async def test_提示词带上文档名与页码(self, fake_excerpts):
        pack = _Pack([_step("Ⅳ级响应", "RESPONDS_WITH", "传达命令", phase="response", label="启动", chunk_id=9)])
        plan = await A.build_answer_plan(None, pack)
        messages = A.build_plan_prompt("怎么应对", plan)
        human = messages[-1][1]
        assert "预案.pdf p.9" in human
        # 规则里必须明确"照抄文档名"，否则模型会写成【文件 p.9】（实测）
        assert "文档名" in messages[0][1]
