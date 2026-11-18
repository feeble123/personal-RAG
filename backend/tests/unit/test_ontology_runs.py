"""O8-2 运行记录回流：一次率定 → Run 节点 + 用到的参数 + 达到的指标。

三条约定在这里守：参数必须已在库、指标可自动建、同一 run_id 幂等。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.db.models import OntologyInstance, OntologyRelation
from app.db.session import async_session_factory
from app.services.graph.ontology import runs
from app.services.graph.ontology.registry import get_registry


async def _cleanup() -> None:
    """只清本文件造的数据（名字都以 t8 开头；Run 的 uid 是哈希，按类型清）。"""
    async with async_session_factory() as db:
        run_uids = [
            r
            for (r,) in (
                await db.execute(
                    select(OntologyInstance.uid).where(
                        OntologyInstance.profile_id == "tuning",
                        OntologyInstance.entity_type == "Run",
                    )
                )
            ).all()
        ]
        if run_uids:
            await db.execute(
                delete(OntologyRelation).where(
                    OntologyRelation.subject_uid.in_(run_uids)
                )
            )
        await db.execute(
            delete(OntologyInstance).where(
                OntologyInstance.profile_id == "tuning",
                OntologyInstance.name.like("t8%"),
            )
        )
        await db.commit()


@pytest_asyncio.fixture
async def seeded(_ensure_test_tables):
    """两个参数实例（参数必须已在库，运行记录不替你造参数）。"""
    await _cleanup()
    reg = get_registry("tuning")
    async with async_session_factory() as db:
        for name, display in (("t8_sm", "t8 自由水蓄水容量"), ("t8_kg", "t8 地下水出流系数")):
            db.add(
                OntologyInstance(
                    profile_id="tuning",
                    uid=reg.uid_for("Parameter", name),
                    entity_type="Parameter",
                    name=name,
                    kb_id=0,
                    props={"display_zh": display, "group": "水源划分", "unit": "-"},
                    source="manual",
                    status="active",
                )
            )
        await db.commit()
    yield reg
    await _cleanup()


class TestRecordRun:
    async def test_建出节点和两类边(self, seeded):
        async with async_session_factory() as db:
            result = await runs.record_run(
                db,
                seeded,
                runs.RunInput(
                    run_id="t8-run-1",
                    started_at="2026-11-18T10:00:00",
                    strategy="SCE-UA",
                    objective="NSE 最大化",
                    parameters={"t8_sm": 22, "t8_kg": 0.35},
                    metrics={"t8_NSE": 0.82},
                ),
            )
            await db.commit()

        assert result.created is True
        assert [u["parameter"] for u in result.used] == ["t8_sm", "t8_kg"]
        assert result.achieved == [{"metric": "t8_NSE", "value": 0.82}]
        # 指标库里没有 → 自动建（跑出来的指标名是事实，不是从文档挖的知识）
        assert result.created_metrics == ["t8_NSE"]
        assert not result.missing_parameters

    async def test_参数不在库里的明确列出来不静默丢(self, seeded):
        async with async_session_factory() as db:
            result = await runs.record_run(
                db,
                seeded,
                runs.RunInput(
                    run_id="t8-run-2",
                    parameters={"t8_sm": 20, "t8_not_exist": 1},
                    metrics={},
                ),
            )
            await db.commit()

        assert [u["parameter"] for u in result.used] == ["t8_sm"]
        assert result.missing_parameters == ["t8_not_exist"]
        assert any("不存在" in w for w in result.warnings)

    async def test_同一_run_id_重复上报不堆节点(self, seeded):
        async with async_session_factory() as db:
            await runs.record_run(
                db, seeded,
                runs.RunInput(run_id="t8-run-3", parameters={"t8_sm": 20}, metrics={"t8_NSE": 0.7}),
            )
            await db.commit()
        async with async_session_factory() as db:
            again = await runs.record_run(
                db, seeded,
                runs.RunInput(run_id="t8-run-3", parameters={"t8_sm": 25}, metrics={"t8_NSE": 0.8}),
            )
            await db.commit()

        # 节点本身没变（没改任何属性）→ 既不是 created 也不是 updated，
        # 但边上的值必须跟上最新一次上报
        assert again.created is False
        assert again.used == [{"parameter": "t8_sm", "value": 25}]
        async with async_session_factory() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(OntologyInstance)
                .where(
                    OntologyInstance.profile_id == "tuning",
                    OntologyInstance.entity_type == "Run",
                    OntologyInstance.name == "t8-run-3",
                )
            )
            assert count == 1

    async def test_重报时旧参数边被清掉(self, seeded):
        """重报少了一个参数：旧边不能留成幽灵边。"""
        async with async_session_factory() as db:
            first = await runs.record_run(
                db, seeded,
                runs.RunInput(
                    run_id="t8-run-4",
                    parameters={"t8_sm": 20, "t8_kg": 0.3},
                    metrics={},
                ),
            )
            await db.commit()
        async with async_session_factory() as db:
            await runs.record_run(
                db, seeded,
                runs.RunInput(run_id="t8-run-4", parameters={"t8_sm": 21}, metrics={}),
            )
            await db.commit()

        async with async_session_factory() as db:
            rels = list(
                (
                    await db.scalars(
                        select(OntologyRelation).where(
                            OntologyRelation.subject_uid == first.run_uid,
                            OntologyRelation.predicate == runs.REL_USED,
                        )
                    )
                ).all()
            )
        assert len(rels) == 1
        assert rels[0].props["value"] == 21

    async def test_库没有运行类型时说清楚(self):
        with pytest.raises(ValueError, match="缺少类型"):
            async with async_session_factory() as db:
                await runs.record_run(
                    db,
                    get_registry("process_skeleton"),
                    runs.RunInput(run_id="t8-x"),
                )

    async def test_run_id_不能为空(self, seeded):
        with pytest.raises(ValueError, match="run_id"):
            async with async_session_factory() as db:
                await runs.record_run(db, seeded, runs.RunInput(run_id="  "))

    async def test_列表按开始时间倒序且带参数指标(self, seeded):
        async with async_session_factory() as db:
            await runs.record_run(
                db, seeded,
                runs.RunInput(
                    run_id="t8-run-old", started_at="2026-11-01T09:00:00",
                    parameters={"t8_sm": 18}, metrics={"t8_NSE": 0.6},
                ),
            )
            await runs.record_run(
                db, seeded,
                runs.RunInput(
                    run_id="t8-run-new", started_at="2026-11-18T09:00:00",
                    parameters={"t8_sm": 22}, metrics={"t8_NSE": 0.8},
                ),
            )
            await db.commit()

        async with async_session_factory() as db:
            rows = await runs.list_runs(db, seeded)
        mine = [r for r in rows if r["name"].startswith("t8-run-")]
        assert [r["name"] for r in mine] == ["t8-run-new", "t8-run-old"]
        assert mine[0]["parameters"][0]["value"] == 22
        assert mine[0]["metrics"][0]["value"] == 0.8
