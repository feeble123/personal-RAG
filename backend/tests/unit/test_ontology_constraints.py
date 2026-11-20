"""约束层：TBox 声明规矩，ABox 求值。

这一层要守住三件事，缺一件约束就会变成摆设：

1. **定义写错要在加载期炸**（`ProfileError`），不能等求值时静默跳过——
   一条永远不会触发的约束比没有约束更危险，因为它给人"有防线"的错觉。
2. **求值只读 active**：archived 的实例和边不参与，否则删过的数据会永远报警。
3. **每条约束都要能追溯到一条 CQ**：`cq` 字段不是装饰，是"这条规矩为什么存在"。
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import constraints as C
from app.services.graph.ontology.registry import ProfileError, load_profile

BASE = """
version: 1
profile_id: probe
domain: test
entity_types:
  - name: Node
    display_name: 节点
    uid_prefix: nd
    scope: local
    attributes:
      - {name: kind, type: string}
      - {name: size, type: number}
  - name: Item
    display_name: 条目
    uid_prefix: it
    scope: local
    attributes:
      - {name: label, type: string}
relation_types:
  - {name: HAS_ITEM, source: Node, target: Item}
  - {name: NEXT, source: Node, target: Node}
"""


class _Row:
    """OntologyInstance 的最小替身。"""

    def __init__(self, uid, entity_type, name, props=None, status="active"):
        self.uid = uid
        self.entity_type = entity_type
        self.name = name
        self.props = props or {}
        self.status = status


class _Rel:
    """OntologyRelation 的最小替身（props 用来放"对应原文第几条"）。"""

    def __init__(self, subject_uid, predicate, object_uid, status="active", props=None):
        self.subject_uid = subject_uid
        self.predicate = predicate
        self.object_uid = object_uid
        self.status = status
        self.props = props or {}


def _profile(tmp_path, constraints: str):
    path = tmp_path / "probe.yaml"
    path.write_text(BASE + "\nconstraints:\n" + constraints, encoding="utf-8")
    return load_profile(path, register=False)


def _nodes(*items):
    return [_Row(f"nd:{name}", "Node", name) for name in items]


def _items(*names):
    return [_Row(f"it:{name}", "Item", name) for name in names]


class TestParse:
    def test_合法约束被完整保留(self, tmp_path):
        reg = _profile(
            tmp_path,
            """
  - id: node.items
    type: min_count
    entity: Node
    relation: HAS_ITEM
    target_type: Item
    min: 2
    severity: warning
    cq: CQ-10
    message: 条目要够
""",
        )
        con = reg.constraint("node.items")
        assert con is not None
        assert con.type == "min_count"
        assert con.relation == ("HAS_ITEM",)
        assert con.target_type == "Item"
        assert con.min == 2
        assert con.severity == "warning"
        assert con.cq == "CQ-10"
        assert reg.to_dict()["constraints"][0]["id"] == "node.items"

    def test_旧_profile_没写_id_也能加载(self, tmp_path):
        # tuning.yaml 里的老写法：{type: required_attribute, entity: Parameter, ...}
        reg = _profile(
            tmp_path,
            "  - {type: required_attribute, entity: Node, attribute: kind}\n",
        )
        assert list(reg.constraints) == ["required_attribute:Node.kind"]

    def test_非法类型直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="type 非法"):
            _profile(tmp_path, "  - {id: x, type: riddle, entity: Node}\n")

    def test_未定义实体直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="未定义实体"):
            _profile(tmp_path, "  - {id: x, type: unique_attribute, entity: Ghost, attribute: a}\n")

    def test_min_count_缺关系直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="必须声明至少一个 relation"):
            _profile(tmp_path, "  - {id: x, type: min_count, entity: Node, min: 1}\n")

    def test_关系主语与实体不符直接报错(self, tmp_path):
        # HAS_ITEM 的主语是 Node；拿 Item 当 entity 配 direction=out 是写错了方向
        with pytest.raises(ProfileError, match="与 entity=Item 不符"):
            _profile(
                tmp_path,
                "  - {id: x, type: min_count, entity: Item, relation: HAS_ITEM, min: 1}\n",
            )

    def test_direction_in_要求实体是宾语(self, tmp_path):
        # HAS_ITEM 的宾语是 Item，direction=in 合法；NEXT 的宾语只能是 Node，Item 不合法
        reg = _profile(
            tmp_path,
            "  - {id: ok, type: min_count, entity: Item, relation: HAS_ITEM,"
            " direction: in, min: 1}\n",
        )
        assert reg.constraint("ok").direction == "in"
        with pytest.raises(ProfileError, match="与 direction=in 不符"):
            _profile(
                tmp_path,
                "  - {id: bad, type: min_count, entity: Item, relation: NEXT,"
                " direction: in, min: 1}\n",
            )

    def test_target_type_必须是关系的另一端(self, tmp_path):
        with pytest.raises(ProfileError, match="target_type Node 不是"):
            _profile(
                tmp_path,
                "  - {id: x, type: min_count, entity: Node, relation: HAS_ITEM,"
                " target_type: Node, min: 1}\n",
            )

    def test_缺_id_重复直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="重复"):
            _profile(
                tmp_path,
                "  - {id: dup, type: unique_attribute, entity: Node, attribute: kind}\n"
                "  - {id: dup, type: unique_attribute, entity: Node, attribute: size}\n",
            )

    def test_min_count_缺下限直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="必须声明 min 或 min_attr"):
            _profile(
                tmp_path, "  - {id: x, type: min_count, entity: Node, relation: HAS_ITEM}\n"
            )

    def test_min_attr_必须是已声明属性(self, tmp_path):
        with pytest.raises(ProfileError, match="min_attr 引用了未定义属性"):
            _profile(
                tmp_path,
                "  - {id: x, type: min_count, entity: Node, relation: HAS_ITEM,"
                " min_attr: ghost}\n",
            )

    def test_非法严重级直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="severity"):
            _profile(
                tmp_path,
                "  - {id: x, type: unique_attribute, entity: Node, attribute: kind,"
                " severity: fatal}\n",
            )


class TestMinCount:
    def test_低于下限报错并给出缺口数量(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: HAS_ITEM, min: 2}\n",
        )
        instances = _nodes("甲") + _items("a")
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a")]
        out = C.check_constraints(reg, instances, relations)
        assert len(out) == 1
        assert out[0].severity == "error"
        assert out[0].subject == "甲"
        assert out[0].detail == {"actual": 1, "expected": 2, "missing": 1}

    def test_达标不报(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: HAS_ITEM, min: 2}\n",
        )
        instances = _nodes("甲") + _items("a", "b")
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a"), _Rel("nd:甲", "HAS_ITEM", "it:b")]
        assert C.check_constraints(reg, instances, relations) == []

    def test_只数指定目标的类型(self, tmp_path):
        # target_type=Item 时，NEXT（指向 Node）不该被算进 ITEM 的数量
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: [HAS_ITEM, NEXT],"
            " target_type: Item, min: 1}\n",
        )
        instances = _nodes("甲", "乙")
        relations = [_Rel("nd:甲", "NEXT", "nd:乙")]
        out = C.check_constraints(reg, instances, relations)
        assert {v.subject for v in out} == {"甲", "乙"}

    def test_多个关系求和(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.out, type: min_count, entity: Node, relation: [HAS_ITEM, NEXT],"
            " min: 1}\n",
        )
        instances = _nodes("甲", "乙") + _items("a")
        relations = [_Rel("nd:甲", "NEXT", "nd:乙")]
        out = C.check_constraints(reg, instances, relations)
        # 甲有一条 NEXT，两条关系取并集，达标；乙一条都没有
        assert {v.subject for v in out} == {"乙"}

    def test_min_attr_按实例属性定下限(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: HAS_ITEM,"
            " min: 1, min_attr: size}\n",
        )
        instances = [
            _Row("nd:甲", "Node", "甲", {"size": 3}),
            _Row("nd:乙", "Node", "乙", {"size": 1}),
        ] + _items("a")
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a"), _Rel("nd:乙", "HAS_ITEM", "it:a")]
        out = C.check_constraints(reg, instances, relations)
        # 甲要求 3 条只有 1 条，报；乙要求 1 条正好 1 条，不报
        assert [v.subject for v in out] == ["甲"]
        assert out[0].detail["expected"] == 3

    def test_声明了min_attr但实例没写要报出来而不是静默跳过(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: HAS_ITEM,"
            " min: 1, min_attr: size}\n",
        )
        instances = _nodes("甲") + _items("a")
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a")]
        out = C.check_constraints(reg, instances, relations)
        assert len(out) == 1
        assert "缺少 size 属性" in out[0].message

    def test_direction_in_数指向自己的边(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: item.used, type: min_count, entity: Item, relation: HAS_ITEM,"
            " direction: in, min: 1}\n",
        )
        instances = _nodes("甲") + _items("a", "b")
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a")]
        out = C.check_constraints(reg, instances, relations)
        assert [v.subject for v in out] == ["b"]


class TestMaxCountAndUnique:
    def test_超过上限报错(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.single, type: max_count, entity: Node, relation: NEXT, max: 1}\n",
        )
        instances = _nodes("甲", "乙", "丙")
        relations = [_Rel("nd:甲", "NEXT", "nd:乙"), _Rel("nd:甲", "NEXT", "nd:丙")]
        out = C.check_constraints(reg, instances, relations)
        assert len(out) == 1
        assert out[0].detail["excess"] == 1

    def test_属性取值重复报错(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.unique_kind, type: unique_attribute, entity: Node, attribute: kind}\n",
        )
        instances = [
            _Row("nd:甲", "Node", "甲", {"kind": "k"}),
            _Row("nd:乙", "Node", "乙", {"kind": "k"}),
            _Row("nd:丙", "Node", "丙", {"kind": "other"}),
        ]
        out = C.check_constraints(reg, instances, [])
        assert len(out) == 1
        assert set(out[0].detail["instances"]) == {"甲", "乙"}

    def test_必填属性为空报错(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.kind, type: required_attribute, entity: Node, attribute: kind}\n",
        )
        instances = [_Row("nd:甲", "Node", "甲", {"kind": ""}), _Row("nd:乙", "Node", "乙", {"kind": "k"})]
        out = C.check_constraints(reg, instances, [])
        assert [v.subject for v in out] == ["甲"]


class TestScopeAndOrder:
    def test_archived_的实例与边都不参与(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: HAS_ITEM, min: 1}\n",
        )
        instances = [_Row("nd:甲", "Node", "甲"), _Row("nd:乙", "Node", "乙", status="archived")]
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a", status="archived")]
        out = C.check_constraints(reg, instances, relations)
        assert [v.subject for v in out] == ["甲"]
        assert out[0].detail["actual"] == 0

    def test_error_排在_warning_前面(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: b.warn, type: min_count, entity: Node, relation: NEXT, min: 1,"
            " severity: warning}\n"
            "  - {id: a.err, type: unique_attribute, entity: Node, attribute: kind}\n",
        )
        instances = [
            _Row("nd:甲", "Node", "甲", {"kind": "k"}),
            _Row("nd:乙", "Node", "乙", {"kind": "k"}),
        ]
        out = C.check_constraints(reg, instances, [])
        assert [v.severity for v in out] == ["error", "warning", "warning"]

    def test_汇总(self, tmp_path):
        reg = _profile(
            tmp_path,
            "  - {id: node.items, type: min_count, entity: Node, relation: HAS_ITEM, min: 1}\n",
        )
        out = C.check_constraints(reg, _nodes("甲", "乙"), [])
        summary = C.summarize(out)
        assert summary["total"] == 2
        assert summary["errors"] == 2
        assert summary["by_constraint"] == {"node.items": 2}


class TestCoverage:
    """逐条核对：为什么不能只数条数。

    实测踩过的坑：Ⅳ级原文 5 条，本体连了 6 个节点看着达标，其实是把第 (4) 条
    拆成"巡查 + 汇报"两个节点凑出了数量，同时把第 (2) 条整条漏掉。
    min_count 查不出来，coverage 查得出来——这是它存在的全部理由。
    """

    def _profile(self, tmp_path):
        return _profile(
            tmp_path,
            """
  - id: node.has_item
    type: coverage
    entity: Node
    relation: HAS_ITEM
    target_type: Item
    total_attr: size
    item_attr: source_item
""",
        )

    def test_拆分成多条但漏了某一项要报出来(self, tmp_path):
        reg = self._profile(tmp_path)
        instances = [_Row("nd:甲", "Node", "甲", {"size": 3})] + _items("a", "b", "c")
        # 第 1 条拆成两个节点，第 2 条整个漏掉
        relations = [
            _Rel("nd:甲", "HAS_ITEM", "it:a", props={"source_item": 1}),
            _Rel("nd:甲", "HAS_ITEM", "it:b", props={"source_item": 1}),
            _Rel("nd:甲", "HAS_ITEM", "it:c", props={"source_item": 3}),
        ]
        out = C.check_constraints(reg, instances, relations)
        assert len(out) == 1
        assert out[0].detail["missing_items"] == [2]
        assert out[0].detail["covered_items"] == [1, 3]
        assert "(2)" in out[0].message

    def test_逐条覆盖就不报(self, tmp_path):
        reg = self._profile(tmp_path)
        instances = [_Row("nd:甲", "Node", "甲", {"size": 2})] + _items("a", "b")
        relations = [
            _Rel("nd:甲", "HAS_ITEM", "it:a", props={"source_item": 1}),
            _Rel("nd:甲", "HAS_ITEM", "it:b", props={"source_item": 2}),
        ]
        assert C.check_constraints(reg, instances, relations) == []

    def test_没标注序号的边要报出来而不是忽略(self, tmp_path):
        reg = self._profile(tmp_path)
        instances = [_Row("nd:甲", "Node", "甲", {"size": 2})] + _items("a", "b")
        relations = [
            _Rel("nd:甲", "HAS_ITEM", "it:a", props={"source_item": 1}),
            _Rel("nd:甲", "HAS_ITEM", "it:b"),  # 没标
        ]
        out = C.check_constraints(reg, instances, relations)
        assert len(out) == 1
        assert out[0].detail["untagged"] == ["b"]
        assert out[0].detail["missing_items"] == [2]

    def test_条目号超出范围也要报(self, tmp_path):
        reg = self._profile(tmp_path)
        instances = [_Row("nd:甲", "Node", "甲", {"size": 1})] + _items("a", "b")
        relations = [
            _Rel("nd:甲", "HAS_ITEM", "it:a", props={"source_item": 1}),
            _Rel("nd:甲", "HAS_ITEM", "it:b", props={"source_item": 9}),
        ]
        out = C.check_constraints(reg, instances, relations)
        assert len(out) == 1
        assert out[0].detail["covered_items"] == [1, 9]
        assert "超出" in out[0].message

    def test_实例没写总数要报出来(self, tmp_path):
        reg = self._profile(tmp_path)
        out = C.check_constraints(reg, _nodes("甲"), [])
        assert len(out) == 1
        assert "缺少 size 属性" in out[0].message

    def test_archived_的边不参与核对(self, tmp_path):
        reg = self._profile(tmp_path)
        instances = [_Row("nd:甲", "Node", "甲", {"size": 1})] + _items("a")
        relations = [_Rel("nd:甲", "HAS_ITEM", "it:a", status="archived", props={"source_item": 1})]
        out = C.check_constraints(reg, instances, relations)
        assert out[0].detail["missing_items"] == [1]


class TestCoverageParse:
    def test_缺_total_attr直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="必须声明 total_attr"):
            _profile(
                tmp_path,
                "  - {id: x, type: coverage, entity: Node, relation: HAS_ITEM,"
                " item_attr: source_item}\n",
            )

    def test_缺_item_attr直接报错(self, tmp_path):
        with pytest.raises(ProfileError, match="必须声明 item_attr"):
            _profile(
                tmp_path,
                "  - {id: x, type: coverage, entity: Node, relation: HAS_ITEM,"
                " total_attr: size}\n",
            )

    def test_total_attr_必须是已声明属性(self, tmp_path):
        with pytest.raises(ProfileError, match="total_attr 引用了未定义属性"):
            _profile(
                tmp_path,
                "  - {id: x, type: coverage, entity: Node, relation: HAS_ITEM,"
                " total_attr: ghost, item_attr: source_item}\n",
            )

    def test_coverage_不该写_min_max(self, tmp_path):
        with pytest.raises(ProfileError, match="不用写 min/max"):
            _profile(
                tmp_path,
                "  - {id: x, type: coverage, entity: Node, relation: HAS_ITEM,"
                " total_attr: size, item_attr: source_item, min: 1}\n",
            )


class TestReservoirProfile:
    """真实 profile 的约束必须真的存在——否则前面所有机制都是空的。"""

    def test_七条约束都在且有_cq(self):
        from app.services.graph.ontology import reset_cache

        reset_cache()
        reg = load_profile("reservoir_flood", register=False)
        assert len(reg.constraints) == 7
        for con in reg.constraints.values():
            assert con.cq, f"{con.id} 没写 cq，无法追溯为什么要这条约束"

    def test_冲突约束是_warning_不是_error(self):
        """四份报告本来就会打架，冲突要被指出来，但不该拦住建库。"""
        from app.services.graph.ontology import reset_cache

        reset_cache()
        reg = load_profile("reservoir_flood", register=False)
        con = reg.constraint("condition.conflicting_plans")
        assert con is not None
        assert con.severity == "warning"
        assert con.type == "max_count" and con.max == 1

    def test_措施是逐条核对而不是数条数(self):
        from app.services.graph.ontology import reset_cache

        reset_cache()
        reg = load_profile("reservoir_flood", register=False)
        con = reg.constraint("response_level.measures_coverage")
        assert con is not None and con.type == "coverage"
        assert con.total_attr == "expected_measures"
        assert con.item_attr == "source_item"
        assert con.target_type == "measure"

    def test_真实形态下漏一条会报出具体条目号(self):
        from app.services.graph.ontology import reset_cache

        reset_cache()
        reg = load_profile("reservoir_flood", register=False)
        instances = [
            _Row("rl:2", "response_level", "Ⅱ级响应", {"level": "Ⅱ", "expected_measures": 6})
        ] + [_Row(f"ms:{i}", "measure", f"措施{i}") for i in range(3)]
        relations = [
            _Rel("rl:2", "RESPONDS_WITH", "ms:0", props={"source_item": 1}),
            _Rel("rl:2", "RESPONDS_WITH", "ms:1", props={"source_item": 2}),
            _Rel("rl:2", "RESPONDS_WITH", "ms:2", props={"source_item": 5}),
        ]
        out = C.check_constraints(reg, instances, relations)
        hits = [v for v in out if v.constraint_id == "response_level.measures_coverage"]
        assert len(hits) == 1
        assert hits[0].detail["missing_items"] == [3, 4, 6]
        assert "(3)(4)(6)" in hits[0].message
