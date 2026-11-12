"""实体链接：把自然语言问题对到本体实例 / 图节点上。

规则（宁可少连，不可错连）：

- **英文代号**（sm / ke / cg）按词边界匹配，避免 "sm" 命中 "small"；
- **中文名**（自由水蓄水能力）按子串匹配，但要求覆盖问题里足够长的一段；
- 同一实体多条命中只取最高分；
- 图上实体靠别名表（`graph_entity_aliases`）匹配，别名必须是问题的子串。

打分只用于排序，不代表正确概率：
    英文代号精确出现 1.0 / 中文名整串出现 0.9 / 别名命中 0.8 起
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select

from app.db.models import GraphEntityAlias, OntologyInstance
from app.services.graph.schema import normalize_name

_ASCII_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class LinkedEntity:
    uid: str
    entity_type: str
    name: str
    display: str
    matched: str
    score: float
    source: str  # instance / graph

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "entity_type": self.entity_type,
            "name": self.name,
            "display": self.display,
            "matched": self.matched,
            "score": round(self.score, 3),
            "source": self.source,
        }


def _contains_code(question: str, code: str) -> bool:
    """英文代号按词边界匹配（前后不能是字母/数字/下划线）。"""
    pattern = rf"(?<![a-z0-9_]){re.escape(code)}(?![a-z0-9_])"
    return re.search(pattern, question) is not None


def link_instances(rows: Iterable[OntologyInstance], question: str) -> list[LinkedEntity]:
    """在实例集合里找问题提到的实体。"""
    q = normalize_name(question)
    if not q:
        return []
    best: dict[str, LinkedEntity] = {}

    for row in rows:
        display = str((row.props or {}).get("display_zh") or "")
        candidates: list[tuple[str, float, float]] = []  # (文本, 基础分, 权重)
        if row.name:
            candidates.append((row.name, 1.0, 1.0))
        if display:
            candidates.append((display, 0.9, 0.9))

        for text, base, weight in candidates:
            key = normalize_name(text)
            if not key:
                continue
            if _ASCII_RE.match(key):
                if not _contains_code(q, key):
                    continue
                score = base
            else:
                if key not in q:
                    continue
                # 中文名越长、覆盖问题比例越高越可信
                coverage = len(key) / max(len(q), 1)
                score = base * (0.75 + 0.25 * min(1.0, coverage * 2))
            found = LinkedEntity(
                uid=row.uid,
                entity_type=row.entity_type,
                name=row.name,
                display=display or row.name,
                matched=text,
                score=score * weight,
                source="instance",
            )
            current = best.get(row.uid)
            if current is None or found.score > current.score:
                best[row.uid] = found

    return sorted(best.values(), key=lambda x: -x.score)


async def link_graph_entities(
    session: Any, question: str, *, kb_id: int | None = None, limit: int = 10
) -> list[LinkedEntity]:
    """用别名表把问题里的词对到图节点（防洪链/其他实体）。"""
    q = normalize_name(question)
    if not q:
        return []
    data = normalize_name(q)
    stmt = select(GraphEntityAlias)
    if kb_id is not None:
        stmt = stmt.where(GraphEntityAlias.kb_id.in_([0, kb_id]))
    aliases = (await session.execute(stmt.limit(20000))).scalars().all()

    best: dict[str, LinkedEntity] = {}
    for alias in aliases:
        key = alias.normalized_alias or ""
        if len(key) < 2 or key not in data:
            continue
        # 别名占问题的比例越高越可信
        coverage = len(key) / max(len(data), 1)
        score = 0.8 + 0.2 * min(1.0, coverage * 2)
        found = LinkedEntity(
            uid=alias.node_uid,
            entity_type=alias.entity_type,
            name=alias.name,
            display=alias.alias or alias.name,
            matched=alias.alias or alias.name,
            score=score,
            source="graph",
        )
        current = best.get(alias.node_uid)
        if current is None or found.score > current.score:
            best[alias.node_uid] = found

    return sorted(best.values(), key=lambda x: -x.score)[:limit]
