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


def _step(subject, predicate, obj, *, phase, label, chunk_id, props=None, subject_props=None):
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
        "subject_props": subject_props or {},
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


class TestAnswerOrganization:
    """答案怎么组织：扇出合并、按档位分章。

    用户的原话是「读起来像两份文档拼贴」——根因不是事实不够，是组织方式不对。
    """

    async def test_扇出的影响合并成一条(self, fake_excerpts):
        # 「通知下游镇街」扇出 4 个镇，不合并就是 4 段几乎一样的话（实测）
        pack = _Pack(
            [
                _step("Ⅱ级响应", "RESPONDS_WITH", "通知下游镇街", phase="response", label="启动", chunk_id=1),
                _step("通知下游镇街", "AFFECTS", "圣灯山镇", phase="impact", label="影响", chunk_id=2),
                _step("通知下游镇街", "AFFECTS", "安澜镇", phase="impact", label="影响", chunk_id=3),
            ]
        )
        plan = await A.build_answer_plan(None, pack)
        assert len(plan.steps) == 1
        assert plan.steps[0].props["影响"] == "圣灯山镇、安澜镇"

    async def test_按档位分章(self, fake_excerpts):
        pack = _Pack(
            [
                _step("50~1000年一遇", "TRIGGERS", "Ⅱ级响应", phase="basis", label="触发", chunk_id=1),
                _step("Ⅱ级响应", "RESPONDS_WITH", "传达命令", phase="response", label="启动", chunk_id=2),
            ]
        )
        plan = await A.build_answer_plan(None, pack)
        sections = plan.sections()
        assert [s["phase"] for s in sections] == ["basis", "response"]
        assert [len(s["steps"]) for s in sections] == [1, 1]

    async def test_骨架里带章节标题(self, fake_excerpts):
        pack = _Pack(
            [
                _step("50~1000年一遇", "TRIGGERS", "Ⅱ级响应", phase="basis", label="触发", chunk_id=1),
                _step("Ⅱ级响应", "RESPONDS_WITH", "传达命令", phase="response", label="启动", chunk_id=2),
            ]
        )
        plan = await A.build_answer_plan(None, pack)
        text = A.render_plan(plan)
        assert "【工况与依据】" in text
        assert "【怎么应对】" in text
        # 章节标题不能挤进环节正文
        assert "[工况与依据] " not in text

    async def test_同一档连续出现时只出一个标题(self, fake_excerpts):
        pack = _Pack(
            [
                _step("Ⅱ级响应", "RESPONDS_WITH", "传达命令", phase="response", label="启动", chunk_id=1),
                _step("Ⅱ级响应", "RESPONDS_WITH", "专家组到场", phase="response", label="启动", chunk_id=2),
            ]
        )
        plan = await A.build_answer_plan(None, pack)
        assert A.render_plan(plan).count("【怎么应对】") == 1


class TestFocusText:
    """把切片收窄到引句附近。

    预案每一级有 6 条措施，几乎都来自同一个切片（实测 424~436 字）。
    全量灌进去的结果是 10 个环节拿到同一段话，真正的差异被稀释。
    """

    def test_定位到引句时只取附近窗口(self):
        text = "无关开头" * 100 + "初期洪水控制下泄流量130m³/s" + "无关结尾" * 100
        out = A._focus_text(text, "初期洪水控制下泄流量130m³/s")
        assert "130m³/s" in out
        assert len(out) < len(text)
        assert out.startswith("…") and out.endswith("…")

    def test_mineru_把数字拆开也能定位(self):
        # MinerU 会把 `130 m³/s` 拆成 `1 3 0 m^3 / s`
        text = "无关开头" * 100 + "控制下泄流量为 1 3 0 m^3 / s ，直至" + "无关结尾" * 100
        out = A._focus_text(text, "控制下泄流量为 130m³/s")
        assert "1 3 0" in out

    def test_找不到引句就退回原文开头而不是乱猜(self):
        text = "甲" * 500
        assert A._focus_text(text, "这句原文里根本没有") == text[: A.MAX_EXCERPT_CHARS]

    def test_太短的引句不参与定位(self):
        # 两三个字的引句到处都能命中，用它定位会把窗口定到无关位置
        text = "甲" * 500
        assert A._focus_text(text, "甲乙") == text[: A.MAX_EXCERPT_CHARS]

    def test_没有引句时按上限截断(self):
        text = "乙" * 5000
        assert len(A._focus_text(text, "")) == A.MAX_EXCERPT_CHARS


class TestExcerptDedupe:
    """同一段原文在提示词里只出现一次。

    实测 100 年一遇的骨架里，10 个环节共用同一页切片；
    不去重的话提示词 59% 是同一段话。去重键用**渲染出来的正文**而不是 chunk_id，
    因为定位到不同引句时取的是不同窗口，那是有效信息。
    """

    def _plan(self, texts):
        steps = []
        for idx, text in enumerate(texts, start=1):
            steps.append(
                A.AnswerStep(
                    phase="response",
                    phase_label="怎么应对",
                    sentence=f"步骤{idx}",
                    excerpts=[
                        {
                            "chunk_id": 9,
                            "doc_name": "预案.pdf",
                            "printed_page": 52,
                            "text": text,
                        }
                    ],
                )
            )
        return A.AnswerPlan(question="q", steps=steps)

    def test_完全相同的正文只展开一次(self):
        text = A.render_plan(self._plan(["相同的正文", "相同的正文"]))
        assert text.count("相同的正文") == 1
        assert "同一段原文已在上文给出" in text

    def test_不同窗口各自展开(self):
        text = A.render_plan(self._plan(["窗口甲", "窗口乙"]))
        assert "窗口甲" in text and "窗口乙" in text
        assert "同一段原文已在上文给出" not in text


class TestSubjectIdentity:
    """链路"终端主语"的身份要讲出来（CQ-18）。

    问「Ⅳ级响应是谁启动的」，只写「蒋捷 —启动→ Ⅳ级响应」等于把问题重复一遍；
    「领导小组副组长、城南公司董事长」才是答案。但已经作为宾语出现过的节点
    （响应等级、某条措施）属性早就在前面讲过了，再挂一遍就是重复。
    """

    async def test_终端主语的属性被并入(self, fake_excerpts):
        steps = [
            _step("Ⅳ级响应", "RESPONDS_WITH", "加强安全巡查", phase="response", label="启动", chunk_id=1),
            _step(
                "蒋捷", "STARTS", "Ⅳ级响应", phase="actor", label="启动", chunk_id=2,
                subject_props={"职务": "领导小组副组长", "所在单位": "城南公司"},
            ),
        ]
        plan = await A.build_answer_plan(None, _Pack(steps))
        row = next(s for s in plan.steps if s.sentence.startswith("蒋捷"))
        assert row.props["职务"] == "领导小组副组长"
        assert row.props["所在单位"] == "城南公司"

    async def test_当过宾语的节点不重复讲属性(self, fake_excerpts):
        """「Ⅳ级响应」既是 STARTS 的宾语、又是 RESPONDS_WITH 的主语，不该再说一遍。"""
        steps = [
            _step(
                "蒋捷", "STARTS", "Ⅳ级响应", phase="actor", label="启动", chunk_id=2,
                subject_props={"职务": "副组长"},
            ),
            _step(
                "Ⅳ级响应", "RESPONDS_WITH", "加强安全巡查", phase="response", label="启动",
                chunk_id=1, subject_props={"响应级别": "Ⅳ"},
            ),
        ]
        plan = await A.build_answer_plan(None, _Pack(steps))
        row = next(s for s in plan.steps if s.sentence.startswith("Ⅳ级响应"))
        assert "响应级别" not in row.props

    async def test_宾语属性同名时不覆盖(self, fake_excerpts):
        steps = [
            _step(
                "蒋捷", "STARTS", "Ⅳ级响应", phase="actor", label="启动", chunk_id=2,
                props={"职务": "宾语上的值"},
                subject_props={"职务": "主语上的值"},
            ),
            _step("Ⅳ级响应", "RESPONDS_WITH", "加强安全巡查", phase="response", label="启动", chunk_id=1),
        ]
        plan = await A.build_answer_plan(None, _Pack(steps))
        row = next(s for s in plan.steps if s.sentence.startswith("蒋捷"))
        assert row.props["职务"] == "宾语上的值"
