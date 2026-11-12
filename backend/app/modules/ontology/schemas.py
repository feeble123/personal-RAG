"""本体管理接口的输入/输出结构。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EntityTypeStr = str


class InstanceOut(BaseModel):
    """一条本体实例（真相源在 PG）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: str
    profile_id: str
    entity_type: str
    name: str
    kb_id: int
    props: dict[str, Any] = Field(default_factory=dict)
    ranges: list[Any] | None = None
    evidence: list[Any] | None = None
    source: str
    status: str
    updated_at: datetime | None = None


class InstanceCreate(BaseModel):
    entity_type: EntityTypeStr = Field(..., description="实体类型，如 Parameter")
    name: str = Field(..., min_length=1, max_length=200)
    profile_id: str | None = Field(None, description="为空则用 ONTOLOGY_PROFILE")
    kb_id: int = Field(0, ge=0, description="0 = 全局共享")
    props: dict[str, Any] = Field(default_factory=dict)
    ranges: list[Any] = Field(default_factory=list)
    evidence: list[Any] = Field(default_factory=list)
    source: str = Field("manual", description="manual / import / extracted")
    status: Literal["active", "archived"] = "active"


class InstancePatch(BaseModel):
    """只改传了的字段；都要改就都传。"""

    props: dict[str, Any] | None = None
    ranges: list[Any] | None = None
    evidence: list[Any] | None = None
    status: Literal["active", "archived"] | None = None
    source: str | None = None


class CardOut(BaseModel):
    parameter: str
    name: str
    group: str
    unit: str
    range_level: str
    confidence: float
    evidence_level: str = "none"
    range: list[float | None] | None = None
    physical_range: list[float | None] | None = None
    initial: float | None = None
    sensitivity: str | None = None
    narrowed_by: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    dry_run: bool
    profile_id: str
    rows: int
    drafts: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    preview: list[dict[str, Any]] = Field(default_factory=list)


class ReloadOut(BaseModel):
    profile_id: str
    entity_types: int
    relation_types: int
