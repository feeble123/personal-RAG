"""7-C C-2 按文档整体扫：挑切片、分批调模型、跨批次串、如实报覆盖。

最要紧的一条不是"能提出东西"，而是**不能说谎**：

- 范围内 500 段只扫了 80 段，就得明说 500 和 80，否则用户会以为整份文档都
  提过了；
- 前一批提的实例，后一批的关系要能连过去，否则同一座水库的实体散在几批里
  就永远连不起来；
- 一批模型调用失败不该毁掉整轮。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import Chunk, Document, DocumentVersion, KnowledgeBase, OntologyProposal
from app.services.graph.ontology import proposal_runs
from app.services.graph.ontology.anchoring import Candidate
from app.services.graph.ontology.proposals import ProposalRunResult, save_proposals
from app.services.graph.ontology.registry import (
    AttributeDef,
    EntityTypeDef,
    OntologyRegistry,
    RelationTypeDef,
)


def _registry() -> OntologyRegistry:
    reg = OntologyRegistry(
        profile_id="test_flood",
        version=1,
        domain="flood",
        entity_types={
            "response_level": EntityTypeDef(
                name="response_level",
                display_name="响应等级",
                uid_prefix="rl",
                scope="global",
                label="ResponseLevel",
                attributes={
                    "level": AttributeDef(
                        name="level", type="enum", values=("Ⅰ", "Ⅱ", "Ⅲ"), required=True
                    )
                },
            ),
            "measure": EntityTypeDef(
                name="measure",
                display_name="处置措施",
                uid_prefix="ms",
                scope="local",
                label="Measure",
                attributes={},
            ),
            "management_unit": EntityTypeDef(
                name="management_unit",
                display_name="责任单位",
                uid_prefix="mu",
                scope="global",
                label="Unit",
                attributes={},
            ),
        },
        relation_types={
            "RESPONDS_WITH": RelationTypeDef(
                name="RESPONDS_WITH", source="response_level", target="measure"
            ),
            "PERFORMED_BY": RelationTypeDef(
                name="PERFORMED_BY", source="measure", target="management_unit"
            ),
        },
    )
    reg.register()
    return reg


CHUNK1 = (
    "4.6.1.2 Ⅱ级响应。当库水位达到 1012.50 m 时，启动Ⅱ级响应，"
    "由防汛办通知安澜镇人民政府组织转移。"
)
CHUNK2 = "Ⅱ级响应应采取以下措施：加强水情监测、开启泄洪闸控泄。"


def _chunk(cid: int, content: str, idx: int = 0) -> Candidate:
    return Candidate(
        chunk_id=cid,
        doc_id=1,
        doc_name="调度规程.pdf",
        page=idx + 1,
        section=None,
        content=content,
        printed_page=None,
    )


class FakeLLM:
    """按顺序回放给定的回答；Exception 表示这一次调用失败。"""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[-1][1])
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply)


def _reply(instances=None, relations=None) -> str:
    import json

    return json.dumps(
        {"instances": instances or [], "relations": relations or []}, ensure_ascii=False
    )


LEVEL = {
    "type": "response_level",
    "name": "Ⅱ级响应",
    "props": {"level": "Ⅱ"},
    "chunk": 1,
    "quote": "当库水位达到 1012.50 m 时，启动Ⅱ级响应",
}
MEASURE = {
    "type": "measure",
    "name": "加强水情监测",
    "props": {},
    "chunk": 1,
    "quote": "加强水情监测",
}
LINK = {
    "subject": "Ⅱ级响应",
    "predicate": "RESPONDS_WITH",
    "object": "加强水情监测",
    "chunk": 1,
    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
}


@pytest.fixture(autouse=True)
def _tables(_ensure_test_tables):
    """本文件会真查测试库（挑切片、落待审区），先进 schema。"""
    return _ensure_test_tables


@pytest.fixture
def fake_scan(monkeypatch):
    """把切片来源换掉，专测编排逻辑（真正的 DB 挑选另有测试）。"""

    def install(chunks, total=None):
        async def _scan(db, registry, *, kb_id=None, doc_ids=None, limit=80):
            return chunks, (total if total is not None else len(chunks))

        monkeypatch.setattr(proposal_runs, "scan_chunks", _scan)

    return install


async def _run(llm, **kwargs):
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        return await proposal_runs.run_document_scan(
            db, _registry(), llm=llm, **kwargs
        )


class TestScoreChunk:
    def test_命中实体类型名的段得分更高(self):
        reg = _registry()
        hot = proposal_runs.score_chunk("Ⅱ级响应的处置措施与责任单位", reg)
        cold = proposal_runs.score_chunk("今天天气很好，适合施工", reg)
        assert hot > 0
        assert cold == 0

    def test_越具体的名字权重越高(self):
        """「响应等级」比「措施」长，命中它更说明这段在讲本体关心的事。"""
        reg = _registry()
        assert proposal_runs.score_chunk("响应等级", reg) > proposal_runs.score_chunk(
            "措施", reg
        )

    def test_目录页要压到最后(self):
        """目录里列着全部章节标题，实体名一个不落，得分反而最高——
        实测 80 段里混进 4 段封面/目录，每段都白花一次模型调用。"""
        reg = _registry()
        toc = (
            "第一章 响应等级 ............... 12\n"
            "第二章 处置措施 ............... 30\n"
            "第三章 责任单位 ............... 45"
        )
        assert proposal_runs.is_front_matter(toc)
        assert proposal_runs.score_chunk(toc, reg) < proposal_runs.score_chunk(
            "今天天气很好", reg
        )
        assert proposal_runs.is_front_matter("某某讲师某某硕士", section="目录/前言")


class TestRunDocumentScan:
    async def test_跨批次的实例能被后一批的关系连上(self, fake_scan):
        """一批只送 4 段，同一座水库的实体常散在几批里——记不住就永远连不起来。"""
        fake_scan([_chunk(1, CHUNK1), _chunk(2, CHUNK2, 1)])
        llm = FakeLLM([_reply(instances=[LEVEL]), _reply(instances=[MEASURE], relations=[LINK])])
        instances, relations, result = await _run(llm, batch_size=1, max_calls=10)
        assert len(instances) == 2
        assert len(relations) == 1
        assert relations[0].object_uid == instances[1].uid
        assert result.scanned == 2

    async def test_同一实例跨批次重复提只留一条(self, fake_scan):
        fake_scan([_chunk(1, CHUNK1), _chunk(2, CHUNK1, 1)])
        llm = FakeLLM([_reply(instances=[LEVEL]), _reply(instances=[LEVEL])])
        instances, _, result = await _run(llm, batch_size=1)
        assert len(instances) == 1
        assert result.reject_reasons.get("duplicate_instance") == 1
        # 提示要说"本次运行已经提过了"，不能让人去库里找一个还没入库的东西
        assert any("本次运行" in n for n in result.notes)

    async def test_没扫的部分要说清楚(self, fake_scan):
        """范围内 500 段只扫了 2 段，就明说 500 和 2——否则用户以为整份文档提过了。"""
        fake_scan([_chunk(1, CHUNK1), _chunk(2, CHUNK2, 1)], total=500)
        llm = FakeLLM([_reply(instances=[LEVEL]), _reply()])
        _, _, result = await _run(llm, batch_size=1)
        assert result.chunks == 500
        assert result.scanned == 2
        assert any("500" in n and "2" in n for n in result.notes)

    async def test_调用次数到上限要停下并说明(self, fake_scan):
        fake_scan([_chunk(i, CHUNK1, i) for i in range(1, 6)])
        llm = FakeLLM([_reply(instances=[LEVEL]) for _ in range(10)])
        _, _, result = await _run(llm, batch_size=1, max_calls=2)
        assert len(llm.prompts) == 2
        assert result.scanned == 2
        assert any("上限" in n for n in result.notes)

    async def test_一批调用失败不影响后面(self, fake_scan):
        fake_scan([_chunk(1, CHUNK1), _chunk(2, CHUNK2, 1)])
        llm = FakeLLM([RuntimeError("超时"), _reply(instances=[MEASURE])])
        instances, _, result = await _run(llm, batch_size=1)
        assert len(instances) == 1
        assert any("调用失败" in n for n in result.notes)
        # 失败的那批不算扫过
        assert result.scanned == 1

    async def test_没有可扫的切片时明确说明(self, fake_scan):
        fake_scan([], total=0)
        _, _, result = await _run(FakeLLM([]))
        assert result.chunks == 0
        assert any("没有可扫的切片" in n for n in result.notes)


class TestScanChunks:
    """真正查库挑切片：短段丢掉、只取 active 版本、按原文顺序排。"""

    async def _seed(self, name: str, chunks: list[str], *, retired_extra: str | None = None):
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            kb = KnowledgeBase(name=name, status="ready")
            db.add(kb)
            await db.flush()
            doc = Document(
                kb_id=kb.id, filename=f"{name}.pdf", stored_path="x.pdf", file_type="pdf"
            )
            db.add(doc)
            await db.flush()
            ver = DocumentVersion(document_id=doc.id, status="active")
            db.add(ver)
            await db.flush()
            doc.active_version_id = ver.id
            for i, text in enumerate(chunks):
                db.add(
                    Chunk(
                        kb_id=kb.id,
                        doc_id=doc.id,
                        document_version_id=ver.id,
                        chunk_index=i,
                        content=text,
                        content_hash=f"{name}-{i}",
                    )
                )
            if retired_extra:
                old = DocumentVersion(document_id=doc.id, status="retired")
                db.add(old)
                await db.flush()
                db.add(
                    Chunk(
                        kb_id=kb.id,
                        doc_id=doc.id,
                        document_version_id=old.id,
                        chunk_index=0,
                        content=retired_extra,
                        content_hash=f"{name}-old",
                    )
                )
            await db.commit()
            return kb.id, doc.id

    async def _scan(self, kb_id, limit=80):
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            return await proposal_runs.scan_chunks(
                db, _registry(), kb_id=kb_id, limit=limit
            )

    async def test_太短的切片不进预算(self):
        """页眉、目录条目只有十几个字，不值得花一次模型调用。"""
        long_a = CHUNK1 * 3
        kb_id, _ = await self._seed("scan_short", [long_a, "目 录", long_a + "甲"])
        chunks, total = await self._scan(kb_id)
        assert total == 2
        assert all(len(c.content) >= proposal_runs._MIN_CHUNK_CHARS for c in chunks)

    async def test_只扫active版本(self):
        """库里有 active + retired 两套切片，扫到旧版等于把过期内容建成实例。"""
        kb_id, _ = await self._seed(
            "scan_version", [CHUNK1 * 3], retired_extra=CHUNK2 * 10
        )
        chunks, total = await self._scan(kb_id)
        assert total == 1
        assert len(chunks) == 1
        assert "开启泄洪闸控泄" not in chunks[0].content

    async def test_超预算时按相关度挑但结果仍按原文顺序(self):
        """模型读乱序的段落，关系很容易连错——挑完要按原文顺序排回去。"""
        plain = "施工组织设计中的土方开挖与混凝土浇筑要求。" * 5
        hot = "响应等级的处置措施：加强水情监测。" * 5
        kb_id, _ = await self._seed("scan_rank", [plain, hot, plain + "乙", hot + "丙"])
        chunks, total = await self._scan(kb_id, limit=2)
        assert total == 4
        assert len(chunks) == 2
        assert all("响应等级" in c.content for c in chunks)
        # 按 chunk_index 升序：第二段一定在最后
        assert chunks[0].chunk_id < chunks[-1].chunk_id


class TestSaveProposals:
    async def test_新实例和新关系按kind落库且去重(self):
        from app.db.session import async_session_factory
        from app.services.graph.ontology import proposals

        inst = proposals.InstanceCandidate(
            entity_type="response_level",
            name="Ⅱ级响应",
            uid="rl:test1",
            props={"level": "Ⅱ"},
            candidate=_chunk(1, CHUNK1),
            quote="启动Ⅱ级响应",
        )
        rel = proposals.RelationCandidate(
            subject_uid="rl:test1",
            predicate="RESPONDS_WITH",
            object_uid="ms:test2",
            subject_name="Ⅱ级响应",
            object_name="加强水情监测",
            candidate=_chunk(1, CHUNK1),
            quote="启动Ⅱ级响应",
        )
        async with async_session_factory() as db:
            first = await save_proposals(db, "save_test", [inst], [rel])
            await db.commit()
            second = await save_proposals(db, "save_test", [inst], [rel])
            await db.commit()
            rows = (
                await db.scalars(
                    select(OntologyProposal).where(OntologyProposal.profile_id == "save_test")
                )
            ).all()
        assert first == 2
        assert second == 0  # 同一条事实反复跑不堆积
        kinds = sorted(r.kind for r in rows)
        assert kinds == ["instance", "relation"]
        by_kind = {r.kind: r for r in rows}
        assert by_kind["instance"].payload["name"] == "Ⅱ级响应"
        assert by_kind["relation"].payload["predicate"] == "RESPONDS_WITH"
        assert by_kind["instance"].evidence["quote"] == "启动Ⅱ级响应"
