"""冲突仲裁：同一工况下两条结论打架时，按权威级取高者，并把没采用的也说出来。

四份报告里有一处真的打架：5~10 年一遇控泄，调度规程写 120 m³/s、
汛期调度运用计划写 125。以前两条边并存，模型可能把两个数都抄进答案，
读者无从判断该执行哪个。这一层要守住：

1. **可以仲裁才仲裁**——有一条没声明权威级就不猜；
2. **同级不猜**——两个同等效力的文件冲突，是要人定的，程序标出来就好；
3. **不悄悄丢掉**——被淘汰的方案必须出现在回答里，附上数值和理由。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.graph.ontology import answer as A
from app.services.graph.ontology import retrieval as R


def _edge(subject, predicate, obj):
    return SimpleNamespace(subject_uid=subject, predicate_name=predicate, object_uid=obj)


def _node(name, props=None, type_name="dispatch_plan"):
    return SimpleNamespace(name=name, props=props or {}, type_name=type_name)


def _graph(items):
    """items: [(subject_uid, predicate, object_uid, rank|None)]"""
    nodes = {
        "cd:1": _node("5~10年一遇降雨", {"display_zh": "5~10年一遇"}, "condition"),
    }
    edges = []
    for idx, (subject, predicate, obj, rank) in enumerate(items):
        edges.append((1, _edge(subject, predicate, obj)))
        props = {"outflow_m3s": 100 + idx * 25}
        if rank is not None:
            props[R.AUTHORITY_ATTR] = rank
        nodes[obj] = _node(obj.replace("dp:", ""), props)
    return edges, nodes


class TestArbitration:
    def test_按权威级取高者并把另一个记下来(self):
        edges, nodes = _graph(
            [("cd:1", "STIPULATES", "dp:规程", 1), ("cd:1", "STIPULATES", "dp:运用计划", 2)]
        )
        kept, conflicts = R._arbitrate_conflicts(None, edges, nodes)
        assert len(kept) == 1
        assert kept[0][1].object_uid == "dp:运用计划"
        assert len(conflicts) == 1
        item = conflicts[0]
        assert item["resolved"] is True
        assert item["chosen"]["name"] == "运用计划"
        assert [r["name"] for r in item["rejected"]] == ["规程"]
        assert item["rejected"][0]["props"]["outflow_m3s"] == 100
        assert item["chosen"]["props"]["outflow_m3s"] == 125

    def test_有一条没声明权威级就不猜(self):
        edges, nodes = _graph(
            [("cd:1", "STIPULATES", "dp:a", 2), ("cd:1", "STIPULATES", "dp:b", None)]
        )
        kept, conflicts = R._arbitrate_conflicts(None, edges, nodes)
        assert len(kept) == 2, "不能因为一条有级数就擅自淘汰另一条"
        assert conflicts == []

    def test_权威级相同标成无法仲裁且两条都留(self):
        edges, nodes = _graph(
            [("cd:1", "STIPULATES", "dp:a", 1), ("cd:1", "STIPULATES", "dp:b", 1)]
        )
        kept, conflicts = R._arbitrate_conflicts(None, edges, nodes)
        assert len(kept) == 2
        assert conflicts[0]["resolved"] is False
        assert conflicts[0]["chosen"] is None
        assert len(conflicts[0]["rejected"]) == 2
        assert "无法自动仲裁" in conflicts[0]["reason"]

    def test_只有一条边时什么都不做(self):
        edges, nodes = _graph([("cd:1", "STIPULATES", "dp:a", 1)])
        kept, conflicts = R._arbitrate_conflicts(None, edges, nodes)
        assert len(kept) == 1 and conflicts == []

    def test_不同关系之间不互相干扰(self):
        edges, nodes = _graph(
            [
                ("cd:1", "STIPULATES", "dp:a", 1),
                ("cd:1", "STIPULATES", "dp:b", 2),
                ("cd:1", "TRIGGERS", "rl:x", 9),
            ]
        )
        kept, conflicts = R._arbitrate_conflicts(None, edges, nodes)
        # STIPULATES 组仲裁掉一条，TRIGGERS 只有一条原样保留
        assert {(e.predicate_name, e.object_uid) for _h, e in kept} == {
            ("STIPULATES", "dp:b"),
            ("TRIGGERS", "rl:x"),
        }
        assert len(conflicts) == 1


class TestConflictInPlan:
    def _plan(self, conflicts):
        step = A.AnswerStep(
            phase="basis", phase_label="工况与依据", sentence="5~10年一遇 —规定→ 方案"
        )
        plan = A.AnswerPlan(question="怎么调度", steps=[step], conflicts=conflicts)
        return plan

    def test_仲裁过的冲突要同时给出采用值和未采用值(self):
        plan = self._plan(
            [
                {
                    "subject": "5~10年一遇降雨",
                    "predicate_label": "规定",
                    "resolved": True,
                    "chosen": {
                        "name": "运用计划方案",
                        "props": {"控制下泄流量（m³/s）": 125},
                        "authority_rank": 2,
                    },
                    "rejected": [
                        {
                            "name": "规程方案",
                            "props": {"控制下泄流量（m³/s）": 120},
                            "authority_rank": 1,
                        }
                    ],
                }
            ]
        )
        text = A.render_plan(plan)
        assert "采用「运用计划方案」" in text
        assert "未采用「规程方案」" in text
        assert "125" in text and "120" in text
        assert "按权威级取高者" in text

    def test_无法仲裁时要明说需人工确认(self):
        plan = self._plan(
            [
                {
                    "subject": "5~10年一遇降雨",
                    "predicate_label": "规定",
                    "resolved": False,
                    "chosen": None,
                    "rejected": [{"name": "A"}, {"name": "B"}],
                }
            ]
        )
        text = A.render_plan(plan)
        assert "无法自动裁定" in text
        assert "需人工确认" in text

    def test_没有冲突时不出现冲突段落(self):
        assert "存在冲突" not in A.render_plan(self._plan([]))

    def test_提示词里有强制说明的规则(self):
        plan = self._plan([])
        messages = A.build_plan_prompt("怎么调度", plan)
        assert "存在冲突" in messages[0][1], "系统提示词必须要求模型讲出冲突"
