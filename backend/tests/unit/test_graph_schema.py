"""防洪知识图谱：骨架（实体/关系字典、uid、端点校验）单元测试。

骨架是「抽取、台账导入、图存储、检索融合」的共同依据，值一旦定下就锁定，
后续新增类型必须同步改这里与前端常量。
"""
from __future__ import annotations

from app.services.graph.schema import (
    ENTITY_LABELS,
    RELATION_ENDPOINTS,
    RELATION_LABELS,
    Edge,
    EntityType,
    RelationType,
    edge_key,
    endpoints_allowed,
    normalize_name,
    uid_for,
)


class TestEntityTypes:
    def test_eleven_types(self):
        assert len(EntityType) == 11

    def test_flood_chain_types_present(self):
        assert EntityType.CONDITION.value == "condition"
        assert EntityType.MEASURE.value == "measure"

    def test_every_type_has_label(self):
        assert set(ENTITY_LABELS) == set(EntityType)


class TestRelationTypes:
    def test_ten_relations(self):
        assert len(RelationType) == 10

    def test_business_relations(self):
        values = {r.value for r in RelationType}
        assert {"needs_warning", "needs_evacuation", "part_of", "based_on"} <= values

    def test_every_relation_has_label_and_endpoints(self):
        for rel in RelationType:
            assert rel in RELATION_LABELS
            assert rel in RELATION_ENDPOINTS


class TestNormalize:
    def test_strips_quotes_and_spaces(self):
        assert normalize_name("《鲤鱼塘水库》") == "鲤鱼塘水库"
        # 空白全部去掉，保证「鲤鱼塘 水库」与「鲤鱼塘水库」能对上
        assert normalize_name("  鲤鱼塘  水库 ") == "鲤鱼塘水库"

    def test_fullwidth_normalized(self):
        assert normalize_name("ＧＢ５０２０１") == "gb50201"

    def test_empty(self):
        assert normalize_name(None) == ""
        assert normalize_name("") == ""


class TestUid:
    def test_stable(self):
        assert uid_for(EntityType.ENGINEERING_OBJECT, "鲤鱼塘水库", 1) == uid_for(
            EntityType.ENGINEERING_OBJECT, "鲤鱼塘水库", 1
        )

    def test_local_entity_scoped_by_kb(self):
        """工程对象是库内实体：同名不同库必须是两个节点。"""
        a = uid_for(EntityType.ENGINEERING_OBJECT, "XX水库", 1)
        b = uid_for(EntityType.ENGINEERING_OBJECT, "XX水库", 2)
        assert a != b

    def test_global_entity_shared_across_kb(self):
        """行政区是全局实体：跨库同名应合并成一个节点。"""
        a = uid_for(EntityType.ADMIN_REGION, "XX镇", 1)
        b = uid_for(EntityType.ADMIN_REGION, "XX镇", 2)
        assert a == b

    def test_prefix(self):
        assert uid_for(EntityType.CONDITION, "设计工况", 1).startswith("cd:")
        assert uid_for(EntityType.MEASURE, "预警发布", 1).startswith("ms:")
        assert uid_for(EntityType.STANDARD_CLAUSE, "GB50201 4.3", 1).startswith("cl:")


class TestEndpoints:
    def test_valid(self):
        assert endpoints_allowed(
            RelationType.NEEDS_WARNING, EntityType.ADMIN_REGION, EntityType.MEASURE
        )
        assert endpoints_allowed(
            RelationType.PART_OF, EntityType.ENGINEERING_OBJECT, EntityType.ENGINEERING_OBJECT
        )
        assert endpoints_allowed(
            RelationType.DOWNSTREAM_OF, EntityType.RIVER, EntityType.ADMIN_REGION
        )
        assert endpoints_allowed(
            RelationType.STIPULATES, EntityType.CONDITION, EntityType.RIVER
        )

    def test_invalid(self):
        # 条款「位于」行政区毫无意义，必须被拒
        assert not endpoints_allowed(
            RelationType.LOCATED_IN, EntityType.STANDARD_CLAUSE, EntityType.ADMIN_REGION
        )
        # 措施不能「属于」水库
        assert not endpoints_allowed(
            RelationType.PART_OF, EntityType.MEASURE, EntityType.ENGINEERING_OBJECT
        )


class TestEdgeKey:
    def test_format(self):
        assert edge_key("eo:1", RelationType.DOWNSTREAM_OF, "ar:2") == "eo:1|downstream_of|ar:2"

    def test_edge_properties(self):
        edge = Edge(
            subject_uid="eo:1",
            predicate=RelationType.NEEDS_EVACUATION,
            object_uid="ms:2",
            kb_id=3,
        )
        assert edge.key == "eo:1|needs_evacuation|ms:2"
        assert edge.rel_label == "NEEDS_EVACUATION"
        assert edge.to_dict()["predicate_label"] == "NEEDS_EVACUATION"
