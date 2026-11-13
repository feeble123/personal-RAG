"""本体模式进主问答：**事实段渲染** + **本体优先约束**（O7 单元2）。

要点：
- 本体事实单独成段（【本体事实】），排在【参考资料】之前 —— 混进片段里模型会把数值当散文改写
- 参数卡/图谱关系/图谱节点都能渲染成人能看懂的中文
- 有本体事实时追加「以本体为准、不得外推数值」的系统约束
- 没有本体事实时 prompt 与原来**逐字一致**（kb 模式零改动）
"""
from __future__ import annotations

from app.services.chat import build_prompt, format_ontology_facts
from app.services.rag import RetrievedChunk


def _chunk(text: str = "原文片段") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1, kb_id=1, doc_id=2, source="手册.pdf", page=4, snippet=text, score=0.9
    )


def _param_fact() -> dict:
    return {
        "kind": "parameter_prior",
        "subject": "sm",
        "name": "自由水蓄水能力",
        "unit": "mm",
        "range": [5, 50],
        "range_level": "general",
        "initial": 10,
        "evidence_level": "traceable",
        "narrowed_by": ["气候=湿润"],
        "evidence": [
            {"type": "手册", "note": "参数表", "doc_id": 20, "page": 4},
            {"type": "教材", "note": "表5.2", "doc_id": 23, "page": 171},
        ],
    }


def _edge_fact() -> dict:
    return {
        "kind": "graph_edge",
        "subject": "青溪水库",
        "predicate": "downstream_of",
        "predicate_label": "下游",
        "object": "青溪水库下游河段2",
        "source": "graph",
    }


class TestFormatOntologyFacts:
    def test_parameter_card_is_readable(self):
        text = format_ontology_facts([_param_fact()])
        assert "自由水蓄水能力" in text
        assert "5 ~ 50 mm" in text
        assert "建议初值 10" in text
        assert "证据等级 traceable" in text
        # 证据要能点回文档：doc 与页码都得在
        assert "doc=20" in text and "第4页" in text
        assert "doc=23" in text and "第171页" in text

    def test_graph_edge_uses_chinese_label(self):
        text = format_ontology_facts([_edge_fact()])
        assert "青溪水库 下游 青溪水库下游河段2" in text
        assert "downstream_of" not in text

    def test_graph_node_uses_chinese_type(self):
        fact = {
            "kind": "graph_node",
            "name": "青溪水库",
            "entity_type": "engineering_object",
            "entity_type_label": "工程对象",
        }
        assert "青溪水库（工程对象）" in format_ontology_facts([fact])

    def test_empty_facts_render_empty(self):
        assert format_ontology_facts([]) == ""


class TestBuildPromptOntology:
    def test_ontology_section_precedes_references(self):
        messages = build_prompt(
            "sm 一般取多少", [_chunk()], ontology_facts=[_param_fact(), _edge_fact()]
        )
        human = messages[-1][1]
        assert human.index("【本体事实】") < human.index("【参考资料】")
        assert "自由水蓄水能力" in human

    def test_ontology_hit_does_not_warn_missing_refs(self):
        """本体命中且不检索时，参考资料为空是**故意**的。

        写「没有检索到相关参考资料」会被模型读成「知识库未收录」（实测原话），
        把「没查」说成「查了没有」——必须换成中性表述。
        """
        human = build_prompt("sm 一般取多少", [], ontology_facts=[_param_fact()])[-1][1]
        assert "没有检索到相关参考资料" not in human
        assert "本次未检索原文片段" in human

    def test_kb_mode_still_warns_missing_refs(self):
        """kb 模式（真的检索了但没结果）保持原口径不变。"""
        human = build_prompt("sm 一般取多少", [])[-1][1]
        assert "没有检索到相关参考资料" in human

    def test_ontology_constraint_in_system_prompt(self):
        messages = build_prompt("sm 一般取多少", [], ontology_facts=[_param_fact()])
        system = messages[0][1]
        assert "本体优先" in system
        assert "不得编造" in system

    def test_no_ontology_keeps_old_prompt(self):
        """kb 模式零改动：不传 ontology_facts 时既没有事实段，也没有本体约束。"""
        messages = build_prompt("sm 一般取多少", [_chunk()])
        human = messages[-1][1]
        assert "【本体事实】" not in human
        assert "本体优先" not in messages[0][1]
