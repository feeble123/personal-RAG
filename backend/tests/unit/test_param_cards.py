"""参数推荐卡：条件收窄、冲突并列、无依据不出数。"""
from __future__ import annotations

from app.db.models import OntologyInstance
from app.services.graph.ontology.cards import (
    LEVEL_CONFLICT,
    LEVEL_CONDITIONAL,
    LEVEL_GENERAL,
    LEVEL_NONE,
    build_card,
)

_EVIDENCE = [{"type": "教材/手册", "note": "通用取值范围"}]


def _row(**kw) -> OntologyInstance:
    props = {
        "display_zh": "自由水蓄水能力",
        "group": "水源划分",
        "unit": "mm",
        "physical_low": 5,
        "physical_high": 50,
        "range_low": 15,
        "range_high": 30,
        "initial": 20,
        "sensitivity": "高",
    }
    props.update(kw.pop("props", {}))
    return OntologyInstance(
        uid="pn:x",
        profile_id="tuning",
        entity_type="Parameter",
        name=kw.pop("name", "sm"),
        kb_id=kw.pop("kb_id", 0),
        props=props,
        ranges=kw.pop("ranges", None),
        evidence=kw.pop("evidence", _EVIDENCE),
        source="import",
        status="active",
    )


class TestEvidenceRule:
    def test_no_evidence_outputs_no_numbers(self):
        card = build_card(_row(evidence=[]))
        assert card.range_level == LEVEL_NONE
        assert card.confidence == 0.0
        assert card.range is None
        assert card.physical_range is None
        assert card.initial is None
        assert any("没有依据" in n for n in card.notes)

    def test_general_handbook_evidence_adds_note(self):
        card = build_card(_row())
        assert any("通用教材/手册" in n for n in card.notes)

    def test_project_evidence_has_no_generic_note(self):
        card = build_card(_row(evidence=[{"type": "实测", "note": "本流域率定"}]))
        assert not any("通用教材/手册" in n for n in card.notes)


class TestEvidenceLevel:
    def test_traceable_when_chunk_present(self):
        card = build_card(_row(evidence=[{"type": "教材", "note": "x", "doc_id": 3, "chunk_id": 9}]))
        assert card.evidence_level == "traceable"
        assert not any("暂不可溯源" in n for n in card.notes)

    def test_doc_level(self):
        card = build_card(_row(evidence=[{"type": "教材", "note": "x", "doc_id": 3}]))
        assert card.evidence_level == "doc_level"
        assert any("没到具体页码" in n for n in card.notes)

    def test_claim_when_only_text(self):
        card = build_card(_row(evidence=[{"type": "教材", "note": "通用范围"}]))
        assert card.evidence_level == "claim"
        assert any("暂不可溯源" in n for n in card.notes)

    def test_none_when_no_evidence(self):
        assert build_card(_row(evidence=[])).evidence_level == "none"


class TestRangeLevels:
    def test_general_range(self):
        card = build_card(_row())
        assert card.range_level == LEVEL_GENERAL
        assert card.range == [15, 30]
        assert card.physical_range == [5, 50]
        assert card.confidence == 0.60

    def test_conditional_narrowing(self):
        row = _row(ranges=[
            {"low": 15, "high": 25, "when": {"climate": "湿润"}},
            {"low": 20, "high": 35, "when": {"climate": "半干旱"}},
        ])
        card = build_card(row, {"climate": "湿润"})
        assert card.range_level == LEVEL_CONDITIONAL
        assert card.range == [15, 25]
        assert card.narrowed_by == ["climate=湿润"]
        assert card.confidence == 0.80

    def test_condition_silent_falls_back_to_general(self):
        row = _row(ranges=[{"low": 15, "high": 25, "when": {"climate": "湿润"}}])
        card = build_card(row, {"climate": "干旱"})
        assert card.range_level == LEVEL_GENERAL
        assert card.range == [15, 30]
        assert any("没有匹配的收窄范围" in n for n in card.notes)

    def test_multi_condition_requires_all_keys(self):
        row = _row(ranges=[{"low": 10, "high": 20, "when": {"climate": "湿润", "flood_type": "单峰"}}])
        assert build_card(row, {"climate": "湿润"}).range_level == LEVEL_GENERAL
        assert build_card(row, {"climate": "湿润", "flood_type": "单峰"}).range == [10, 20]

    def test_conflicting_matches_are_listed_not_chosen(self):
        row = _row(ranges=[
            {"low": 10, "high": 20, "when": {"climate": "湿润"}},
            {"low": 25, "high": 35, "when": {"climate": "湿润"}},
        ])
        card = build_card(row, {"climate": "湿润"})
        assert card.range_level == LEVEL_CONFLICT
        assert card.range is None
        assert len(card.candidates) == 2
        assert card.confidence == 0.50
        assert any("请人工选择" in n for n in card.notes)

    def test_identical_matches_are_not_conflict(self):
        row = _row(ranges=[
            {"low": 10, "high": 20, "when": {"climate": "湿润"}},
            {"low": 10, "high": 20, "when": {"climate": "湿润"}},
        ])
        assert build_card(row, {"climate": "湿润"}).range_level == LEVEL_CONDITIONAL

    def test_empty_conditions_use_general(self):
        row = _row(ranges=[{"low": 15, "high": 25, "when": {"climate": "湿润"}}])
        assert build_card(row, None).range_level == LEVEL_GENERAL

    def test_no_range_at_all(self):
        card = build_card(_row(props={"range_low": None, "range_high": None}))
        assert card.range_level == LEVEL_NONE
        assert card.range is None
        assert any("没有通用范围" in n for n in card.notes)

    def test_to_dict_shape(self):
        data = build_card(_row()).to_dict()
        assert set(data) >= {
            "parameter", "name", "group", "unit", "range_level", "confidence",
            "range", "physical_range", "initial", "sensitivity", "narrowed_by",
            "candidates", "evidence", "notes",
        }
