"""知识图谱服务（第一期：防洪链）。

模块分工：
- schema.py     实体/关系字典、uid、端点校验（骨架，唯一权威）
- flood_chain.py 人工台账 → 节点/关系（纯函数，可离线测）
- store.py      图存储抽象 + 进程内实现（Neo4j 在后续单元补）

对外保持轻量导入：`from app.services.graph import schema, store, flood_chain`。
"""
from app.services.graph.schema import (
    ENTITY_LABELS,
    ENTITY_LABELS_ZH,
    RELATION_LABELS,
    RELATION_LABELS_ZH,
    Edge,
    EntityType,
    ExtractionResult,
    GraphSlice,
    Node,
    RelationType,
    edge_key,
    endpoints_allowed,
    normalize_name,
    uid_for,
)
from app.services.graph.store import (
    GraphStore,
    InMemoryGraphStore,
    get_graph_store,
    reset_graph_store,
)

__all__ = [
    "ENTITY_LABELS",
    "ENTITY_LABELS_ZH",
    "RELATION_LABELS",
    "RELATION_LABELS_ZH",
    "Edge",
    "EntityType",
    "ExtractionResult",
    "GraphSlice",
    "GraphStore",
    "InMemoryGraphStore",
    "Node",
    "RelationType",
    "edge_key",
    "endpoints_allowed",
    "get_graph_store",
    "normalize_name",
    "reset_graph_store",
    "uid_for",
]
