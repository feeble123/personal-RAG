"""通用本体引擎（Ontology RAG）。

- registry：加载/校验/缓存 YAML profile（TBox），并注册到 graph.schema
- validator：实例与关系校验
- instances：实例读写（ABox 真相源在 PostgreSQL，图库可重建）
- params：参数先验 Excel 的读取与校验

约定：本包代码**不得出现任何具体模型名**——模型知识只能以 profiles/*.yaml
与参数表的形式存在。
"""
from app.services.graph.ontology.registry import (
    DEFAULT_PROFILE,
    PROFILES_DIR,
    AttributeDef,
    EntityTypeDef,
    OntologyRegistry,
    ProfileError,
    RelationTypeDef,
    get_registry,
    load_profile,
    reset_cache,
)
from app.services.graph.ontology.validator import (
    ValidationResult,
    validate_entity,
    validate_relation,
)

__all__ = [
    "DEFAULT_PROFILE",
    "PROFILES_DIR",
    "AttributeDef",
    "EntityTypeDef",
    "OntologyRegistry",
    "ProfileError",
    "RelationTypeDef",
    "ValidationResult",
    "get_registry",
    "load_profile",
    "reset_cache",
    "validate_entity",
    "validate_relation",
]
