"""7-C C-4 提案落地：接受才写库，而且写不进去要说清为什么。

这里守的四条：

1. **提案可以有缺口，落地不行**——缺必填的实例不该被接受，且原因要能看懂；
2. **只补不覆盖**——实例已经存在时，提案只填空字段，人工值一个字不动；
3. **两端不存在就不建边**——悬空边是没法查、也没法审的；
4. **失败留在待审区**——不能把"点接受但没建成"悄悄标成已接受。
"""
from __future__ import annotations

import pytest

from app.db.models import OntologyInstance, OntologyProposal, OntologyRelation
from app.services.graph.ontology import proposal_accept
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
        },
        relation_types={
            "RESPONDS_WITH": RelationTypeDef(
                name="RESPONDS_WITH", source="response_level", target="measure"
            )
        },
    )
    reg.register()
    return reg


def _proposal(**kw) -> OntologyProposal:
    """造一条待审提案（不入库；落地逻辑只读它的字段）。"""
    base = dict(
        id=1,
        profile_id="test_flood",
        instance_uid="rl:new",
        entity_type="response_level",
        attribute="",
        value={},
        kind="instance",
        method="graph",
        confidence=0.6,
        evidence={"type": "图提案", "quote": "启动Ⅱ级响应", "doc_id": 1},
        dedupe_key="k1",
        status="pending",
    )
    base.update(kw)
    return OntologyProposal(**base)


def _instance_payload(name="Ⅱ级响应", uid="rl:new", **props) -> dict:
    return {
        "entity_type": "response_level",
        "name": name,
        "uid": uid,
        "props": {"level": "Ⅱ", **props},
        "kb_id": 0,
    }


def _relation_payload(subject="rl:new", predicate="RESPONDS_WITH", obj="ms:new") -> dict:
    return {
        "subject_uid": subject,
        "predicate": predicate,
        "object_uid": obj,
        "props": {},
        "kb_id": 0,
    }


@pytest.fixture(autouse=True)
def _tables(_ensure_test_tables):
    return _ensure_test_tables


@pytest.fixture(autouse=True)
async def _clean():
    from sqlalchemy import delete

    from app.db.session import async_session_factory

    async def purge():
        async with async_session_factory() as db:
            for model in (OntologyRelation, OntologyInstance):
                await db.execute(delete(model).where(model.profile_id == "test_flood"))
            await db.commit()

    await purge()
    yield
    await purge()


async def _accept(proposal, registry=None):
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        result = await proposal_accept.accept(db, registry or _registry(), proposal)
        await db.commit()
        return result


class TestAcceptInstance:
    async def test_新实例落地并带证据(self):
        result = await _accept(_proposal(payload=_instance_payload()))
        assert result.ok, result.reason

        from sqlalchemy import select

        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            row = await db.scalar(
                select(OntologyInstance).where(OntologyInstance.uid == "rl:new")
            )
        assert row is not None
        assert row.name == "Ⅱ级响应"
        assert row.props["level"] == "Ⅱ"
        assert row.source == "extracted"
        assert row.evidence[0]["quote"] == "启动Ⅱ级响应"

    async def test_缺必填就拒并说清缺什么(self):
        """提案阶段缺了只提醒；真正写进库时必须拦——不然库里多一条不完整的实例。"""
        result = await _accept(
            _proposal(payload=_instance_payload(level=""))  # 必填的 level 空着
        )
        assert not result.ok
        assert "level" in result.reason

        from sqlalchemy import select

        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            assert (
                await db.scalar(
                    select(OntologyInstance).where(OntologyInstance.uid == "rl:new")
                )
            ) is None

    async def test_实例已存在时只补空字段不覆盖人工值(self):
        """人工整理的值比机器猜的可信——接受提案不能把人写的字冲掉。"""
        from sqlalchemy import select

        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            db.add(
                OntologyInstance(
                    profile_id="test_flood",
                    entity_type="response_level",
                    name="Ⅱ级响应",
                    uid="rl:new",
                    kb_id=0,
                    props={"level": "Ⅱ", "note": "人工写的说明"},
                    evidence=[{"type": "人工", "quote": "人工依据"}],
                    source="manual",
                    status="active",
                )
            )
            await db.commit()

        result = await _accept(
            _proposal(payload=_instance_payload(level="Ⅲ", note="模型写的说明", owner="当值"))
        )
        assert result.ok

        async with async_session_factory() as db:
            row = await db.scalar(
                select(OntologyInstance).where(OntologyInstance.uid == "rl:new")
            )
        assert row.props["level"] == "Ⅱ"          # 人工值没被动
        assert row.props["note"] == "人工写的说明"   # 人工值没被动
        assert row.props["owner"] == "当值"          # 空字段补上了
        quotes = [e.get("quote") for e in row.evidence]
        assert "人工依据" in quotes and "启动Ⅱ级响应" in quotes  # 证据是并集


class TestAcceptRelation:
    async def _seed_instances(self):
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            db.add(
                OntologyInstance(
                    profile_id="test_flood", entity_type="response_level",
                    name="Ⅱ级响应", uid="rl:new", kb_id=0, props={"level": "Ⅱ"},
                    evidence=[], source="manual", status="active",
                )
            )
            db.add(
                OntologyInstance(
                    profile_id="test_flood", entity_type="measure",
                    name="加强水情监测", uid="ms:new", kb_id=0, props={},
                    evidence=[], source="manual", status="active",
                )
            )
            await db.commit()

    async def test_两端都在就建成(self):
        await self._seed_instances()
        result = await _accept(
            _proposal(kind="relation", instance_uid="rl:new", payload=_relation_payload())
        )
        assert result.ok, result.reason

        from sqlalchemy import select

        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            row = await db.scalar(
                select(OntologyRelation).where(OntologyRelation.subject_uid == "rl:new")
            )
        assert row is not None
        assert row.predicate == "RESPONDS_WITH"
        assert row.evidence[0]["quote"] == "启动Ⅱ级响应"

    async def test_一端没建就拒并指路(self):
        """最常见的场景：同批的新实例还没被接受。自动建它才是越界。"""
        await self._seed_instances()
        result = await _accept(
            _proposal(
                kind="relation",
                instance_uid="rl:new",
                payload=_relation_payload(obj="ms:不存在"),
            )
        )
        assert not result.ok
        assert "ms:不存在" in result.reason
        assert "先把它的「新实例」提案接受了" in result.reason

    async def test_端点不合法就拒(self):
        await self._seed_instances()
        # RESPONDS_WITH 只能 response_level → measure，掉头就非法
        result = await _accept(
            _proposal(
                kind="relation",
                instance_uid="rl:new",
                payload=_relation_payload(subject="ms:new", obj="rl:new"),
            )
        )
        assert not result.ok
        assert "端点非法" in result.reason
