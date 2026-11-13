"""F4 自动抽取：规则命中 → 待审 → 接受才落实例。

覆盖：
- 按属性类型安全转换（转不了就丢弃，绝不写错类型）
- 正则命中取第 1 组；非法正则不炸整轮
- 同一实例同一属性只取第一条命中（避免候选刷屏）
- 待审区去重：同一结论反复跑不堆积
- 接受后才写进实例属性，且证据是**并集**（不覆盖人工写的证据）
"""
from __future__ import annotations

from app.services.graph.ontology import extraction
from app.services.graph.ontology.registry import AttributeDef


class TestCast:
    def test_number(self):
        attr = AttributeDef(name="x", type="number")
        assert extraction._cast("1250", attr) == 1250
        assert extraction._cast("12.5", attr) == 12.5
        assert extraction._cast("1,250", attr) == 1250

    def test_number_rejects_garbage(self):
        attr = AttributeDef(name="x", type="number")
        assert extraction._cast("很大", attr) is None

    def test_enum_must_be_declared(self):
        attr = AttributeDef(name="x", type="enum", values=("高", "中", "低"))
        assert extraction._cast("高", attr) == "高"
        assert extraction._cast("很高", attr) is None

    def test_text_passes_through(self):
        attr = AttributeDef(name="x", type="string")
        assert extraction._cast(" 青溪水库 ", attr) == "青溪水库"

    def test_empty(self):
        assert extraction._cast("   ", AttributeDef(name="x", type="string")) is None


class TestRuleMatch:
    def _attr(self):
        return AttributeDef(
            name="safe_discharge_m3s",
            type="number",
            extract={"pattern": r"安全泄量[^0-9]{0,8}([0-9]+(?:\.[0-9]+)?)"},
        )

    def test_hits_first_group_and_keeps_context(self):
        hit = extraction._rule_match("青溪水库安全泄量为 1250 m3/s，校核 1800。", self._attr())
        assert hit is not None
        value, snippet = hit
        assert value == 1250
        assert "1250" in snippet and "安全泄量" in snippet

    def test_no_match(self):
        assert extraction._rule_match("文本里没有这个说法", self._attr()) is None

    def test_broken_regex_does_not_raise(self):
        attr = AttributeDef(name="x", type="number", extract={"pattern": "([0-9"})
        assert extraction._rule_match("123", attr) is None

    def test_value_that_cannot_cast_is_dropped(self):
        attr = AttributeDef(name="x", type="number", extract={"pattern": r"值为\s*(\S+)"})
        assert extraction._rule_match("值为 很多", attr) is None

    def test_map_translates_before_cast(self):
        """原文「敏感」要变成枚举值「高」——靠 extract.map 翻译。"""
        attr = AttributeDef(
            name="sensitivity", type="enum", values=("高", "中", "低"),
            extract={"pattern": r"敏感程度\s*[=＝]\s*(\S{1,4})", "map": {"敏感": "高", "不敏感": "低"}},
        )
        hit = extraction._rule_match("SM 敏感程度=敏感", attr)
        assert hit is not None and hit[0] == "高"
        hit2 = extraction._rule_match("WD 敏感程度=不敏感", attr)
        assert hit2 is not None and hit2[0] == "低"


class TestLlmJson:
    def test_plain_json(self):
        assert extraction._parse_llm_json('{"a": {"value": 1}}') == {"a": {"value": 1}}

    def test_fenced_json(self):
        assert extraction._parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_chatter_around_json(self):
        assert extraction._parse_llm_json('好的：\n{"a": 1}\n以上') == {"a": 1}

    def test_garbage(self):
        assert extraction._parse_llm_json("模型没按格式回答") == {}


class TestDedupeKey:
    def test_same_conclusion_same_key(self):
        a = extraction.Proposal("pn:1", "Parameter", "sm", 5.0)
        b = extraction.Proposal("pn:1", "Parameter", "sm", 5.0, method="llm", confidence=0.5)
        assert a.dedupe_key == b.dedupe_key, "同一条结论（不管谁抽的）只该留一份"

    def test_different_value_different_key(self):
        a = extraction.Proposal("pn:1", "Parameter", "sm", 5.0)
        b = extraction.Proposal("pn:1", "Parameter", "sm", 6.0)
        assert a.dedupe_key != b.dedupe_key


class TestProposalFlow:
    """DB 级：抽出来 → 待审去重 → 接受才写实例，证据取并集。"""

    async def _seed(self, profile_id: str = "tuning"):
        from app.db.models import Chunk, Document, DocumentVersion, KnowledgeBase, OntologyInstance
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            kb = KnowledgeBase(name="抽取测试库", status="ready")
            db.add(kb)
            await db.flush()
            doc = Document(
                kb_id=kb.id, filename="手册.pdf", stored_path="x.pdf",
                file_type="pdf", status="ready",
            )
            db.add(doc)
            await db.commit()
            ver = DocumentVersion(document_id=doc.id, status="active")
            db.add(ver)
            await db.commit()
            doc.active_version_id = ver.id
            db.add(
                Chunk(
                    kb_id=kb.id, doc_id=doc.id, document_version_id=ver.id, chunk_index=0,
                    content="青溪水库安全泄量为 1250 m3/s，校核洪水位 132.5 m。",
                    content_hash="e" * 64, block_type="text", page=14,
                )
            )
            inst = OntologyInstance(
                profile_id=profile_id, uid="pn:test-discharge", entity_type="engineering_object",
                name="青溪水库", props={"display_zh": "青溪水库"}, kb_id=kb.id,
            )
            db.add(inst)
            await db.commit()
            return kb.id, doc.id

    def _registry(self):
        from app.services.graph.ontology.registry import (
            AttributeDef,
            EntityTypeDef,
            OntologyRegistry,
        )

        entity = EntityTypeDef(
            name="engineering_object", display_name="工程对象", uid_prefix="eo",
            scope="local", label="eo",
            attributes={
                "safe_discharge_m3s": AttributeDef(
                    name="safe_discharge_m3s", type="number",
                    extract={"pattern": r"安全泄量[^0-9]{0,8}([0-9]+(?:\.[0-9]+)?)"},
                )
            },
        )
        return OntologyRegistry(
            profile_id="tuning", version=1, domain="test",
            entity_types={"engineering_object": entity}, relation_types={},
        )

    async def test_rule_extraction_and_accept(self, client):
        from sqlalchemy import delete, select

        from app.db.models import KnowledgeBase, OntologyInstance, OntologyProposal
        from app.db.session import async_session_factory

        kb_id, _ = await self._seed()
        async with async_session_factory() as db:
            reg = self._registry()
            proposals = await extraction.run_rule_extraction(db, reg, kb_id=kb_id)
            assert len(proposals) == 1
            p = proposals[0]
            assert p.value == 1250
            assert p.evidence["page"] == 14
            assert p.evidence["doc_id"]

            saved = await extraction.save_proposals(db, "tuning", proposals)
            assert saved == 1
            # 再跑一次不该重复堆积
            again = await extraction.run_rule_extraction(db, reg, kb_id=kb_id)
            assert await extraction.save_proposals(db, "tuning", again) == 0

            row = (await db.scalars(
                select(OntologyProposal).where(OntologyProposal.profile_id == "tuning")
            )).first()
            assert row is not None and row.status == "pending"

            assert await extraction.accept_proposal(db, row) is True
            inst = (await db.scalars(
                select(OntologyInstance).where(OntologyInstance.uid == "pn:test-discharge")
            )).first()
            assert inst is not None
            assert (inst.props or {}).get("safe_discharge_m3s") == 1250
            assert inst.evidence and inst.evidence[0]["page"] == 14
            assert inst.source == "extracted"

        # 清理（外键：先删实例/提案，再删库）
        async with async_session_factory() as db:
            await db.execute(delete(OntologyProposal).where(OntologyProposal.profile_id == "tuning"))
            await db.execute(delete(OntologyInstance).where(OntologyInstance.uid == "pn:test-discharge"))
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            await db.commit()
