"""改关系定义的方向后，已有边要能找出来并掉头。

用户实测的坑：先按「水闸 —管辖→ 管理所」建了边，后来发现方向反了，
把 TBox 定义改成「管理所 —管辖→ 水闸」，**图没变**——因为 TBox 只约束新边，
已有边存的是两端 uid，不会自动反过来。这里补的就是「找出来 + 一键掉头」。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.services.graph.ontology import relations as rel_mod


def _inst(profile: str, uid: str, name: str, entity_type: str):
    from app.db.models import OntologyInstance

    return OntologyInstance(
        profile_id=profile, uid=uid, entity_type=entity_type, name=name,
        props={"display_zh": name}, kb_id=0,
    )


def _rel(profile: str, s: str, pred: str, o: str):
    from app.db.models import OntologyRelation

    return OntologyRelation(
        profile_id=profile, subject_uid=s, predicate=pred, object_uid=o, kb_id=0,
    )


@pytest.fixture
async def seeded(client):
    from app.db.models import OntologyInstance, OntologyRelation
    from app.db.session import async_session_factory

    profile = "tuning"  # 用现成 profile，避免测试里再建库
    async with async_session_factory() as db:
        db.add_all([
            _inst(profile, "gt:test-1", "青溪闸", "gate"),
            _inst(profile, "ms:test-1", "青溪管理所", "manage_station"),
        ])
        db.add(_rel(profile, "gt:test-1", "RESPONSIBLE", "ms:test-1"))
        await db.commit()
    yield profile
    async with async_session_factory() as db:
        await db.execute(
            delete(OntologyRelation).where(OntologyRelation.profile_id == profile)
        )
        await db.execute(
            delete(OntologyInstance).where(OntologyInstance.profile_id == profile)
        )
        await db.commit()


class TestReversedEdges:
    async def test_detects_edge_pointing_the_wrong_way(self, seeded):
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            # 新定义：管理所 —RESPONSIBLE→ 水闸；已有边是 水闸→管理所 → 方向相反
            rows = await rel_mod.find_reversed_edges(
                db, profile_id=seeded, predicate="RESPONSIBLE",
                source_type="manage_station", target_type="gate",
            )
            assert len(rows) == 1
            assert rows[0].subject_uid == "gt:test-1"

    async def test_correct_direction_is_not_flagged(self, seeded):
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            rows = await rel_mod.find_reversed_edges(
                db, profile_id=seeded, predicate="RESPONSIBLE",
                source_type="gate", target_type="manage_station",
            )
            assert rows == []

    async def test_swap_endpoints(self, seeded):
        from app.db.session import async_session_factory

        async with async_session_factory() as db:
            rows = await rel_mod.find_reversed_edges(
                db, profile_id=seeded, predicate="RESPONSIBLE",
                source_type="manage_station", target_type="gate",
            )
            assert await rel_mod.swap_endpoints(db, rows) == 1

            from app.db.models import OntologyRelation

            after = (
                await db.scalars(
                    select(OntologyRelation).where(OntologyRelation.profile_id == seeded)
                )
            ).all()
            assert after[0].subject_uid == "ms:test-1"
            assert after[0].object_uid == "gt:test-1"
            # 掉头之后不该再被判定为反向
            assert await rel_mod.find_reversed_edges(
                db, profile_id=seeded, predicate="RESPONSIBLE",
                source_type="manage_station", target_type="gate",
            ) == []
