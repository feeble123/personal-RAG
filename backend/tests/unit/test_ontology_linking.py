"""实体链接：英文代号按词边界、中文名按子串、同实体取最高分。"""
from __future__ import annotations

from app.db.models import OntologyInstance
from app.services.graph.ontology.linking import link_instances


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
