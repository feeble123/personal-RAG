"""P2 证据锚定：模型只许指认与抄写，不许撰写。

这里守的是最关键的一条：**引用必须逐字出现在原文里**。
校验不过就丢弃——假出处比没出处更坏，用户会拿它当依据去建模。
"""
from __future__ import annotations

from app.services.graph.ontology import anchoring
from app.services.graph.ontology.registry import AttributeDef, EntityTypeDef

CONTENT = (
    "4.模型参数\n"
    "SM  自由水蓄水容量  5～50  默认 10\n"
    "KG  地下水消退系数  0.05～0.999  默认 0.95\n"
)


def _entity() -> EntityTypeDef:
    return EntityTypeDef(
        name="Parameter",
        display_name="参数",
        uid_prefix="pm",
        scope="local",
        label="Parameter",
        attributes={
            "display_zh": AttributeDef(name="display_zh", type="string", description="中文名"),
            "range_low": AttributeDef(name="range_low", type="number", description="通用下限"),
        },
    )


def _candidates() -> list[anchoring.Candidate]:
    return [
        anchoring.Candidate(
            chunk_id=11, doc_id=20, doc_name="新安江模型手册.pdf", page=4, section="4.模型参数",
            content=CONTENT,
        )
    ]


class TestQuoteCheck:
    def test_逐字引用可识别(self):
        assert anchoring.quote_supported("KG  地下水消退系数", CONTENT)

    def test_忽略空白差异(self):
        """PDF 抽出来的换行/空格位置很不稳定，比对前去掉所有空白。"""
        assert anchoring.quote_supported("KG 地下水消退系数 0.05～0.999", CONTENT)

    def test_编造的句子过不了(self):
        assert not anchoring.quote_supported("KG 地下水消退系数一般取 0.9 左右比较好调", CONTENT)

    def test_太短的引用不算(self):
        assert not anchoring.quote_supported("KG", CONTENT)

    def test_值必须出现在引用里(self):
        assert anchoring.value_in_quote(0.95, "KG  地下水消退系数  0.05～0.999  默认 0.95")
        assert anchoring.value_in_quote(5.0, "SM 5～50")  # 5.0 与 5 视为同一个数
        assert not anchoring.value_in_quote(0.8, "KG  地下水消退系数")


class TestBuildHit:
    def test_正常结论通过校验(self):
        result = anchoring.AnchorRunResult()
        hit = anchoring.build_hit(
            {"chunk": 1, "attribute": "range_low", "quote": "KG  地下水消退系数  0.05～0.999", "value": 0.05},
            _candidates(),
            instance_uid="pm:kg",
            entity_type="Parameter",
            entity=_entity(),
            result=result,
        )
        assert hit is not None
        assert hit.value == 0.05
        assert hit.as_evidence()["traceable"] is True
        assert hit.as_evidence()["attributes"] == ["range_low"]

    def test_模型编的引用直接丢弃(self):
        result = anchoring.AnchorRunResult()
        hit = anchoring.build_hit(
            {"chunk": 1, "quote": "本参数一般取 0.9 左右，专家经验值", "value": 0.9},
            _candidates(),
            instance_uid="pm:kg",
            entity_type="Parameter",
            entity=_entity(),
            result=result,
        )
        assert hit is None
        assert result.reject_reasons == {"quote_not_in_chunk": 1}

    def test_值不在引用里就只留证据(self):
        """引用是真的，但模型顺口给了一个原文里没有的数——丢掉值，保留出处。"""
        result = anchoring.AnchorRunResult()
        hit = anchoring.build_hit(
            {"chunk": 1, "quote": "KG  地下水消退系数  0.05～0.999", "value": 0.8},
            _candidates(),
            instance_uid="pm:kg",
            entity_type="Parameter",
            entity=_entity(),
            result=result,
        )
        assert hit is not None
        assert hit.value is None

    def test_片段编号不存在(self):
        result = anchoring.AnchorRunResult()
        assert (
            anchoring.build_hit(
                {"chunk": 9, "quote": "KG  地下水消退系数"},
                _candidates(),
                instance_uid="pm:kg",
                entity_type="Parameter",
                entity=_entity(),
                result=result,
            )
            is None
        )
        assert result.reject_reasons == {"no_chunk": 1}

    def test_字段名不在本体里(self):
        result = anchoring.AnchorRunResult()
        assert (
            anchoring.build_hit(
                {"chunk": 1, "attribute": "magic", "quote": "KG  地下水消退系数"},
                _candidates(),
                instance_uid="pm:kg",
                entity_type="Parameter",
                entity=_entity(),
                result=result,
            )
            is None
        )
        assert result.reject_reasons == {"bad_attribute": 1}


class TestParse:
    def test_能从围栏里抠出_items(self):
        text = '```json\n{"items":[{"chunk":1,"quote":"KG  地下水消退系数"}]}\n```'
        assert anchoring.parse_llm_items(text)[0]["quote"].startswith("KG")

    def test_坏_json_不炸(self):
        assert anchoring.parse_llm_items("模型说：这个我不知道") == []


class TestDedupeKey:
    def test_同一条结论_key_相同_不同字段_key_不同(self):
        a = anchoring.AnchorHit(
            instance_uid="pm:kg", entity_type="Parameter", attribute="range_low",
            candidate=_candidates()[0], quote="KG  地下水消退系数",
        )
        b = anchoring.AnchorHit(
            instance_uid="pm:kg", entity_type="Parameter", attribute="range_low",
            candidate=_candidates()[0], quote="KG  地下水消退系数",
        )
        c = anchoring.AnchorHit(
            instance_uid="pm:kg", entity_type="Parameter", attribute="",
            candidate=_candidates()[0], quote="KG  地下水消退系数",
        )
        assert a.dedupe_key == b.dedupe_key
        assert a.dedupe_key != c.dedupe_key

    def test_同一页被切成重复切片不会重复堆待审(self):
        """手册参数表常被切成几个内容相同的切片：按引用原文去重，别按切片 id。"""
        other = anchoring.Candidate(
            chunk_id=12, doc_id=20, doc_name="新安江模型手册.pdf", page=4, section="4.模型参数",
            content=CONTENT,
        )
        a = anchoring.AnchorHit(
            instance_uid="pm:kg", entity_type="Parameter", attribute="range_low",
            candidate=_candidates()[0], quote="KG  地下水消退系数  0.05～0.999",
        )
        b = anchoring.AnchorHit(
            instance_uid="pm:kg", entity_type="Parameter", attribute="range_low",
            candidate=other, quote="KG 地下水消退系数 0.05～0.999",  # 空白不同、切片不同
        )
        assert a.dedupe_key == b.dedupe_key
