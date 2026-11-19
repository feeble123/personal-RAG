"""实体链接：英文代号按词边界、中文名按子串、同实体取最高分。"""
from __future__ import annotations

import pytest

from app.db.models import GraphEntityAlias, OntologyInstance
from app.services.graph.ontology.linking import link_graph_entities, link_instances


def _row(name: str, display: str = "", uid: str | None = None) -> OntologyInstance:
    return OntologyInstance(
        uid=uid or f"pn:{name}",
        profile_id="tuning",
        entity_type="Parameter",
        name=name,
        kb_id=0,
        props={"display_zh": display} if display else {},
        evidence=[],
        source="import",
        status="active",
    )


ROWS = [
    _row("sm", "自由水蓄水能力"),
    _row("ke", "蒸散发折算系数"),
    _row("cg", "地下水消退系数"),
]


class TestCodeMatching:
    def test_exact_code(self):
        hits = link_instances(ROWS, "sm 取多少合适")
        assert [h.name for h in hits] == ["sm"]
        assert hits[0].score == 1.0

    def test_code_boundary_avoids_false_positive(self):
        """"small basin" 里的 sm 不能被当成参数 sm。"""
        assert link_instances(ROWS, "small basin 的径流") == []

    def test_code_with_underscore_neighbour(self):
        assert link_instances(ROWS, "参数sm_值") == []  # 下划线相连 → 不算独立代号

    def test_multiple_codes(self):
        hits = link_instances(ROWS, "ke 和 cg 怎么调")
        assert {h.name for h in hits} == {"ke", "cg"}


class TestChineseMatching:
    def test_full_name(self):
        hits = link_instances(ROWS, "自由水蓄水能力怎么取")
        assert hits and hits[0].name == "sm"
        assert 0.5 < hits[0].score <= 0.9

    def test_long_question_lower_score(self):
        short = link_instances(ROWS, "自由水蓄水能力")
        long = link_instances(
            ROWS, "我想问一下在湿润地区做洪水预报时自由水蓄水能力这个参数应该怎么取值比较合适"
        )
        assert long[0].score < short[0].score


class TestDedupe:
    def test_same_entity_kept_once_with_best_score(self):
        hits = link_instances(ROWS, "sm 自由水蓄水能力 是多少")
        assert len(hits) == 1
        assert hits[0].score == 1.0  # 英文代号精确匹配分更高

    def test_empty_question(self):
        assert link_instances(ROWS, "") == []
        assert link_instances(ROWS, "   ") == []

    def test_unrelated_question(self):
        assert link_instances(ROWS, "今天天气怎么样") == []


# ---------- 图上链接：别名表之外，还要能按实例名捞到 ----------


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Session:
    """按调用顺序返回：第一次给别名表，第二次给实例表。"""

    def __init__(self, alias_rows, instance_rows):
        self._alias_rows = alias_rows
        self._instance_rows = instance_rows
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _Result(self._alias_rows if self.calls == 1 else self._instance_rows)


def _alias(node_uid: str, alias: str, name: str = "") -> GraphEntityAlias:
    alias_row = GraphEntityAlias(
        node_uid=node_uid,
        entity_type="Parameter",
        name=name or alias,
        alias=alias,
        normalized_alias=alias.lower(),
        kb_id=0,
    )
    return alias_row


@pytest.mark.asyncio
async def test_graph_link_falls_back_to_instance_names():
    """别名表为空时，也要能从本体实例名里认出实体。

    本体接口创建的实例**从来不写别名表**，只认别名等于把这条链路整段丢掉。
    """
    rows = [_row("数据准备", "数据准备阶段")]
    found = await link_graph_entities(_Session([], rows), "数据准备阶段要做哪些事")
    assert [e.name for e in found] == ["数据准备"]
    assert found[0].source == "instance"


@pytest.mark.asyncio
async def test_graph_link_prefers_alias_hit():
    alias = _alias("pn:sm", "自由水蓄水", name="sm")
    rows = [_row("sm", "自由水蓄水能力")]
    found = await link_graph_entities(_Session([alias], rows), "自由水蓄水容量怎么定")
    assert len(found) == 1
    assert found[0].source == "graph"
