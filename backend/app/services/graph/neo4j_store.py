"""Neo4j 图存储（O2）——实现 `GraphStore` 协议。

设计要点（和内存实现保持同一套语义，能互换）：

1. **懒加载 + 降级**：构造时**不连接**。真正用到才建驱动；任何异常（连不上、
   超时、Cypher 报错）都只记日志并返回空结果，绝不把异常抛给主链路——
   「图库挂了，问答照常」是硬要求。
2. **幂等写入**：节点按 `uid` MERGE，边按 (主体, 关系类型, 客体, 版本) MERGE。
   重复导入不会产生重复数据，这是「可以随时重建」的前提。
3. **列表属性并集**：`kb_ids/doc_ids/version_ids/aliases` 用 Cypher 列表推导做并集，
   不依赖 APOC（Neo4j Desktop 社区版默认没有 APOC）。
4. **标量属性单独存一份**：`props` 里有值的键会额外写成 `p_<key>`，
   这样 `MATCH (n) WHERE n.p_group = $g` 能走索引/过滤；完整原值仍留在 `props_json`。
5. **状态优先级**：confirmed > rejected > auto（人工结论永远压过自动抽取）。
6. **版本**：同一三元组不同版本是两条边；`version_key = -1` 表示无版本
   （Cypher 里不能存 null 属性，所以用哨兵值，读出来再还原成 None）。

数据模型：
- 节点 `(:OntologyEntity {uid, entity_type, name, props_json, aliases_text, kb_ids, ...})`
- 关系 `[:ONT_REL {relation_type, version_key, kb_id, doc_id, chunk_ids, evidence_ids,
                   confidence, status, extractor, evidence_text, props_json}]`
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.core.config import settings
from app.services.graph.schema import (
    Edge,
    GraphSlice,
    Node,
    canonical_relation,
    type_value,
)

logger = logging.getLogger(__name__)

NODE_LABEL = "OntologyEntity"
REL_TYPE = "ONT_REL"
FULLTEXT_INDEX = "ont_entity_ft"

# schema 初始化失败后的冷却时间：图库没起来时不要每次调用都重试三条 DDL
_SCHEMA_RETRY_COOLDOWN_S = 30.0
# 连接最长存活时间。太短会导致频繁重建连接（每次约 2 秒），把稳态查询超时顶爆。
_CONNECTION_LIFETIME_S = 3600
# 单条 Cypher 一次带多少行（节点/边批量写入）
_BATCH_SIZE = 500
# 状态优先级：数字越大越权威（与内存实现一致）
_STATUS_RANK = {"auto": 0, "rejected": 1, "confirmed": 2}

_SCHEMA_STATEMENTS = (
    f"CREATE CONSTRAINT ont_entity_uid IF NOT EXISTS "
    f"FOR (n:{NODE_LABEL}) REQUIRE n.uid IS UNIQUE",
    f"CREATE INDEX ont_entity_type IF NOT EXISTS FOR (n:{NODE_LABEL}) ON (n.entity_type)",
    f"CREATE INDEX ont_entity_kb IF NOT EXISTS FOR (n:{NODE_LABEL}) ON (n.kb_ids)",
    f"CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS "
    f"FOR (n:{NODE_LABEL}) ON EACH [n.name, n.aliases_text]",
    f"CREATE INDEX ont_rel_type IF NOT EXISTS FOR ()-[r:{REL_TYPE}]-() ON (r.relation_type)",
    f"CREATE INDEX ont_rel_kb IF NOT EXISTS FOR ()-[r:{REL_TYPE}]-() ON (r.kb_id)",
)

_UPSERT_NODES = f"""
UNWIND $rows AS row
MERGE (n:{NODE_LABEL} {{uid: row.uid}})
ON CREATE SET n.entity_type = row.entity_type,
              n.name = row.name,
              n.props_json = row.props_json,
              n.aliases_text = row.aliases_text,
              n.aliases = row.aliases,
              n.kb_ids = row.kb_ids,
              n.doc_ids = row.doc_ids,
              n.version_ids = row.version_ids,
              n += row.scalars
ON MATCH SET n.entity_type = row.entity_type,
             n.name = row.name,
             n.props_json = row.props_json,
             n.aliases_text = row.aliases_text,
             n.aliases = coalesce(n.aliases, [])
                         + [x IN row.aliases WHERE NOT x IN coalesce(n.aliases, [])],
             n.kb_ids = coalesce(n.kb_ids, [])
                        + [x IN row.kb_ids WHERE NOT x IN coalesce(n.kb_ids, [])],
             n.doc_ids = coalesce(n.doc_ids, [])
                         + [x IN row.doc_ids WHERE NOT x IN coalesce(n.doc_ids, [])],
             n.version_ids = coalesce(n.version_ids, [])
                             + [x IN row.version_ids WHERE NOT x IN coalesce(n.version_ids, [])],
             n += row.scalars
"""

_UPSERT_EDGES = f"""
UNWIND $rows AS row
MATCH (s:{NODE_LABEL} {{uid: row.subject}})
MATCH (t:{NODE_LABEL} {{uid: row.object}})
MERGE (s)-[r:{REL_TYPE} {{relation_type: row.rel, version_key: row.ver}}]->(t)
ON CREATE SET r.kb_id = row.kb_id,
              r.doc_id = row.doc_id,
              r.chunk_ids = row.chunk_ids,
              r.evidence_ids = row.evidence_ids,
              r.confidence = row.confidence,
              r.status = row.status,
              r.extractor = row.extractor,
              r.evidence_text = row.evidence_text,
              r.props_json = row.props_json
ON MATCH SET r.chunk_ids = coalesce(r.chunk_ids, [])
                           + [x IN row.chunk_ids WHERE NOT x IN coalesce(r.chunk_ids, [])],
             r.evidence_ids = coalesce(r.evidence_ids, [])
                              + [x IN row.evidence_ids WHERE NOT x IN coalesce(r.evidence_ids, [])],
             r.confidence = CASE WHEN row.confidence > coalesce(r.confidence, 0.0)
                                 THEN row.confidence ELSE coalesce(r.confidence, 0.0) END,
             r.status = CASE
                 WHEN row.status_rank = 2 THEN 'confirmed'
                 WHEN r.status = 'confirmed' THEN 'confirmed'
                 WHEN row.status_rank = 1 THEN 'rejected'
                 WHEN r.status = 'rejected' THEN 'rejected'
                 ELSE 'auto' END,
             r.evidence_text = CASE WHEN size(coalesce(row.evidence_text, ''))
                                      > size(coalesce(r.evidence_text, ''))
                                    THEN row.evidence_text
                                    ELSE coalesce(r.evidence_text, '') END
"""


class SchemaUnavailableError(RuntimeError):
    """schema 初始化失败后的冷却期内再次调用。"""


# ---------------- 纯函数（好测，不碰网络）----------------


def _lucene_escape(text: str) -> str:
    """转义 Lucene 特殊字符，避免用户输入把全文查询语法搞崩。"""
    specials = set('+-&|!(){}[]^"~*?:\\/')
    return "".join("\\" + ch if ch in specials else ch for ch in str(text or ""))


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_load(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return fallback
    return fallback if raw is None else raw


def _scalar_props(props: dict[str, Any]) -> dict[str, Any]:
    """只挑 Neo4j 能直接做属性的标量（str/int/float/bool），并加 `p_` 前缀。"""
    out: dict[str, Any] = {}
    for key, value in props.items():
        if value is None or isinstance(value, (list, dict, set, tuple)):
            continue
        if isinstance(value, (str, int, float, bool)):
            out[f"p_{key}"] = value
    return out


def _node_row(node: Node) -> dict[str, Any]:
    names = [node.name, *[a for a in node.aliases if a != node.name]]
    return {
        "uid": node.uid,
        "entity_type": node.type_name,
        "name": node.name,
        "props_json": _json_dump(node.props),
        "aliases_text": " ".join(n for n in names if n),
        "aliases": names,
        "kb_ids": list(node.kb_ids),
        "doc_ids": list(node.doc_ids),
        "version_ids": list(node.version_ids),
        "scalars": _scalar_props(node.props),
    }


def _edge_row(edge: Edge) -> dict[str, Any]:
    return {
        "subject": edge.subject_uid,
        "object": edge.object_uid,
        "rel": canonical_relation(edge.predicate_name),
        "ver": -1 if edge.version_id is None else int(edge.version_id),
        "kb_id": edge.kb_id,
        "doc_id": edge.doc_id,
        "chunk_ids": list(edge.chunk_ids),
        "evidence_ids": list(edge.evidence_ids),
        "confidence": float(edge.confidence),
        "status": edge.status,
        "status_rank": _STATUS_RANK.get(edge.status, 0),
        "extractor": edge.extractor,
        "evidence_text": edge.evidence_text,
        "props_json": _json_dump(edge.props),
    }


def _as_props(value: Any) -> dict[str, Any] | None:
    """把驱动的 Node/Relationship 对象或普通 dict 统一成属性字典。

    注意：**不能用 `result.data()`** —— neo4j 驱动 6.x 会把关系转成
    `(起始节点, 类型, 结束节点)` 三元组并**丢掉关系属性**，边的 chunk_ids/status
    全都读不到。所以统一用 `dict(record)`，节点/关系保持实体对象，再由这里取属性。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        return dict(value)  # neo4j.Node / neo4j.Relationship 都支持 dict()
    except (TypeError, ValueError):
        return None


def _node_from_props(raw: Any) -> Node | None:
    props = _as_props(raw)
    if not props:
        return None
    raw = props
    uid = raw.get("uid")
    if not uid:
        return None
    return Node(
        uid=str(uid),
        entity_type=raw.get("entity_type") or "",
        name=raw.get("name") or "",
        props=_json_load(raw.get("props_json"), {}),
        aliases=[str(a) for a in (raw.get("aliases") or [])],
        kb_ids=[int(x) for x in (raw.get("kb_ids") or []) if x is not None],
        doc_ids=[int(x) for x in (raw.get("doc_ids") or []) if x is not None],
        version_ids=[int(x) for x in (raw.get("version_ids") or []) if x is not None],
    )


def _edge_from_props(subject_uid: str, object_uid: str, raw: Any) -> Edge | None:
    raw = _as_props(raw)
    if not raw or not subject_uid or not object_uid:
        return None
    ver = raw.get("version_key")
    try:
        version_id = None if ver is None or int(ver) < 0 else int(ver)
    except (TypeError, ValueError):
        version_id = None
    confidence = raw.get("confidence")
    return Edge(
        subject_uid=str(subject_uid),
        predicate=raw.get("relation_type") or "",
        object_uid=str(object_uid),
        kb_id=int(raw.get("kb_id") or 0),
        doc_id=raw.get("doc_id"),
        version_id=version_id,
        chunk_ids=[int(x) for x in (raw.get("chunk_ids") or []) if x is not None],
        evidence_ids=[int(x) for x in (raw.get("evidence_ids") or []) if x is not None],
        props=_json_load(raw.get("props_json"), {}),
        extractor=raw.get("extractor") or "rule",
        confidence=float(confidence) if confidence is not None else 1.0,
        status=raw.get("status") or "auto",
        evidence_text=raw.get("evidence_text") or "",
    )


def _chunks(rows: list[Any], size: int = _BATCH_SIZE) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)] or []


# ---------------- 存储实现 ----------------


class Neo4jGraphStore:
    """Neo4j 实现。所有方法都「不抛异常」，失败返回空结果。"""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        query_timeout_s: float | None = None,
        connect_timeout_s: float | None = None,
    ) -> None:
        self._uri = uri or settings.graph_neo4j_uri
        self._user = user or settings.graph_neo4j_user
        self._password = password if password is not None else settings.graph_neo4j_password
        self._database = database or settings.graph_neo4j_database
        timeout_ms = query_timeout_s if query_timeout_s is not None else settings.graph_query_timeout_ms / 1000
        self._timeout_s = max(0.2, float(timeout_ms))
        self._connect_timeout_s = max(
            1.0,
            float(connect_timeout_s if connect_timeout_s is not None else settings.graph_neo4j_connect_timeout_s),
        )
        # schema 初始化是一次性动作（建约束/索引），比单次查询慢得多：
        # 冷启动时驱动首次握手实测约 2 秒，用查询超时会误判成「图库不可用」
        self._schema_timeout_s = max(self._connect_timeout_s, 10.0)
        self._driver_obj: Any = None
        self._schema_ready = False
        # 连接池是否已经热过。建连（含回收后重建）约 2 秒，远大于稳态查询超时，
        # 所以只有「热着」的时候才敢用 0.8 秒的紧超时。
        self._warm = False
        self._schema_retry_at = 0.0
        self._schema_lock = asyncio.Lock()

    # ---------- 连接与 schema ----------
    def _driver(self) -> Any:
        if self._driver_obj is None:
            from neo4j import AsyncGraphDatabase  # 延迟导入：不启用图库时零开销

            self._driver_obj = AsyncGraphDatabase.driver(
                self._uri,
                auth=(self._user, self._password),
                connection_timeout=self._connect_timeout_s,
                # 原来 300 秒回收一次：每 5 分钟就要重新握手（实测 ~2 秒），
                # 正好撞上 0.8 秒的查询超时，表现为「图谱用一会儿就没边了」。
                max_connection_lifetime=_CONNECTION_LIFETIME_S,
                max_connection_pool_size=int(settings.graph_neo4j_pool_size),
                # 池子被占满时默认要等 60 秒，会把调用方一起拖死
                connection_acquisition_timeout=self._connect_timeout_s,
            )
        return self._driver_obj

    async def ensure_schema(self) -> bool:
        """显式初始化 schema（冒烟脚本/运维用）；失败返回 False，不抛。"""
        try:
            await self._ensure_schema()
            return True
        except Exception as exc:  # noqa: BLE001 - 降级是设计行为
            logger.warning("Neo4j schema 初始化失败：%s", exc)
            return False

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            if time.monotonic() < self._schema_retry_at:
                raise SchemaUnavailableError("Neo4j schema 初始化失败冷却中")
            try:
                await asyncio.wait_for(self._init_schema(), timeout=self._schema_timeout_s)
            except Exception:
                # 冷却：图库没起来时，不要每次调用都重试六条 DDL
                self._schema_retry_at = time.monotonic() + _SCHEMA_RETRY_COOLDOWN_S
                raise
            self._schema_ready = True

    async def _init_schema(self) -> None:
        async with self._driver().session(database=self._database) as session:
            for statement in _SCHEMA_STATEMENTS:
                result = await session.run(statement)
                await result.consume()

    async def _execute(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        await self._ensure_schema()
        async with self._driver().session(database=self._database) as session:
            result = await session.run(cypher, params)
            # 用 dict(record) 而不是 result.data()：后者会把关系压成三元组、丢关系属性
            return [dict(record) async for record in result]

    def _budget(self) -> float:
        """这次调用能用多少秒。

        只有「schema 建好 + 连接池热着」才用稳态的紧超时；其余情况（冷启动、
        刚失败过）一律给宽松预算——建连本身就要 ~2 秒，用 0.8 秒只会反复误判。
        """
        steady = self._schema_ready and self._warm
        return self._timeout_s if steady else self._schema_timeout_s

    def _note_failure(self) -> None:
        """记录一次失败：schema 还没建好就进入冷却，避免图库宕机时每次调用都等超时。"""
        # 关键：失败后把「连接池已热」标记清掉，下一次调用改用宽松超时重建连接。
        # 否则一次 2 秒的建连超时之后，后面每次都被 0.8 秒的稳态超时卡死，永远连不回来
        # —— 实测就是「图能看，但过几分钟所有边都没了」。
        self._warm = False
        if not self._schema_ready:
            self._schema_retry_at = time.monotonic() + _SCHEMA_RETRY_COOLDOWN_S

    async def _run(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行查询：超时/异常一律降级为空结果，不打断调用方。"""
        # 首次调用要顺带建 schema + 驱动握手（实测约 2 秒），不能用稳态查询超时去卡它，
        # 否则进程刚起来的头几个图查询会被误判成「图库不可用」并触发 30 秒冷却。
        # 连接池被回收后重建连接同样要 ~2 秒，所以「上一次失败过」也走宽松超时。
        timeout = self._budget()
        try:
            rows = await asyncio.wait_for(self._execute(cypher, params or {}), timeout=timeout)
            self._warm = True
            return rows
        except asyncio.TimeoutError:
            self._note_failure()
            logger.warning("Neo4j 查询超时（>%.2fs），本次返回空结果", timeout)
        except SchemaUnavailableError:
            # 冷却期内：真正的原因上一次已经按 WARNING 记过，这里不重复刷屏
            logger.debug("Neo4j schema 冷却中，跳过本次查询")
        except Exception as exc:  # noqa: BLE001 - 降级是设计行为
            self._note_failure()
            logger.warning("Neo4j 不可用（%s: %s），本次返回空结果", exc.__class__.__name__, exc)
        return []

    # ---------- 写入 ----------
    async def upsert(self, nodes: list[Node], edges: list[Edge]) -> None:
        for group in _chunks([_node_row(n) for n in nodes]):
            await self._run(_UPSERT_NODES, {"rows": group})
        # 边必须晚于节点写：MATCH 依赖节点已存在
        for group in _chunks([_edge_row(e) for e in edges]):
            await self._run(_UPSERT_EDGES, {"rows": group})

    # ---------- 删除 ----------
    async def delete_version(self, doc_id: int, version_id: int) -> None:
        await self._run(
            f"MATCH ()-[r:{REL_TYPE}]->() "
            f"WHERE r.doc_id = $doc_id AND r.version_key = $ver DELETE r",
            {"doc_id": doc_id, "ver": int(version_id)},
        )
        await self._run(
            f"MATCH (n:{NODE_LABEL}) "
            f"WHERE NOT (n)-[:{REL_TYPE}]-() "
            f"  AND $ver IN coalesce(n.version_ids, []) "
            f"  AND size([x IN coalesce(n.version_ids, []) WHERE x <> $ver]) = 0 "
            f"DELETE n",
            {"ver": int(version_id)},
        )

    async def delete_document(self, doc_id: int) -> None:
        await self._run(
            f"MATCH ()-[r:{REL_TYPE}]->() WHERE r.doc_id = $doc_id DELETE r",
            {"doc_id": doc_id},
        )
        await self._run(
            f"MATCH (n:{NODE_LABEL}) "
            f"WHERE NOT (n)-[:{REL_TYPE}]-() "
            f"  AND $doc_id IN coalesce(n.doc_ids, []) "
            f"  AND size([x IN coalesce(n.doc_ids, []) WHERE x <> $doc_id]) = 0 "
            f"DELETE n",
            {"doc_id": doc_id},
        )

    async def delete_kb(self, kb_id: int) -> None:
        # 用有向模式：无向模式会让同一条边匹配两次
        await self._run(
            f"MATCH ()-[r:{REL_TYPE}]->() WHERE r.kb_id = $kb_id DELETE r",
            {"kb_id": kb_id},
        )
        await self._run(
            f"MATCH (n:{NODE_LABEL}) "
            f"WHERE $kb_id IN coalesce(n.kb_ids, []) "
            f"  AND size([x IN coalesce(n.kb_ids, []) WHERE x <> $kb_id]) = 0 "
            f"  AND NOT (n)-[:{REL_TYPE}]-() "
            f"DELETE n",
            {"kb_id": kb_id},
        )

    async def delete_nodes(self, uids: list[str]) -> None:
        """DETACH DELETE：连边一起删，不留悬空关系。"""
        if not uids:
            return
        await self._run(
            f"MATCH (n:{NODE_LABEL}) WHERE n.uid IN $uids DETACH DELETE n",
            {"uids": list(uids)},
        )

    async def delete_edges(self, subject_uid: str, predicate: str, object_uid: str) -> None:
        await self._run(
            f"MATCH (a:{NODE_LABEL} {{uid: $s}})-[r:{REL_TYPE}]->(b:{NODE_LABEL} {{uid: $o}}) "
            f"WHERE r.relation_type = $p DELETE r",
            {"s": subject_uid, "p": canonical_relation(predicate), "o": object_uid},
        )

    # ---------- 读取 ----------
    async def get_node(self, uid: str) -> Node | None:
        rows = await self._run(
            f"MATCH (n:{NODE_LABEL} {{uid: $uid}}) RETURN n LIMIT 1", {"uid": uid}
        )
        return _node_from_props(rows[0].get("n")) if rows else None

    async def neighbors(
        self,
        uid: str,
        depth: int = 1,
        rel_types: list[Any] | None = None,
        limit: int = 200,
    ) -> GraphSlice:
        depth = max(1, min(int(depth), 2))  # 只允许 1~2 跳，防爆炸
        types = [canonical_relation(t) for t in rel_types] if rel_types else None
        params: dict[str, Any] = {"uid": uid, "types": types, "limit": int(limit)}
        if depth == 1:
            cypher = (
                f"MATCH (a:{NODE_LABEL} {{uid: $uid}})-[r:{REL_TYPE}]-(b:{NODE_LABEL}) "
                f"WHERE coalesce(r.status, 'auto') <> 'rejected' "
                f"  AND ($types IS NULL OR r.relation_type IN $types) "
                f"RETURN a, r, b, startNode(r).uid AS src, endNode(r).uid AS dst "
                f"LIMIT $limit"
            )
            rows = await self._run(cypher, params)
            return self._slice(rows, edge_keys=(("r", "src", "dst"),), limit=limit)

        cypher = (
            f"MATCH (a:{NODE_LABEL} {{uid: $uid}})-[r1:{REL_TYPE}]-(m:{NODE_LABEL})"
            f"-[r2:{REL_TYPE}]-(b:{NODE_LABEL}) "
            f"WHERE coalesce(r1.status, 'auto') <> 'rejected' "
            f"  AND coalesce(r2.status, 'auto') <> 'rejected' "
            f"  AND ($types IS NULL OR (r1.relation_type IN $types AND r2.relation_type IN $types)) "
            f"RETURN a, m, b, r1, r2, "
            f"       startNode(r1).uid AS src1, endNode(r1).uid AS dst1, "
            f"       startNode(r2).uid AS src2, endNode(r2).uid AS dst2 "
            f"LIMIT $limit"
        )
        rows = await self._run(cypher, params)
        return self._slice(
            rows, edge_keys=(("r1", "src1", "dst1"), ("r2", "src2", "dst2")), limit=limit
        )

    @staticmethod
    def _slice(
        rows: list[dict[str, Any]],
        edge_keys: tuple[tuple[str, str, str], ...],
        limit: int,
    ) -> GraphSlice:
        nodes: dict[str, Node] = {}
        edges: dict[tuple, Edge] = {}
        for row in rows:
            for value in row.values():
                props = _as_props(value)
                if props and "uid" in props:
                    node = _node_from_props(props)
                    if node is not None:
                        nodes.setdefault(node.uid, node)
            for rel_key, src_key, dst_key in edge_keys:
                edge = _edge_from_props(row.get(src_key, ""), row.get(dst_key, ""), row.get(rel_key))
                if edge is not None:
                    edges[(edge.subject_uid, edge.predicate_name, edge.object_uid, edge.version_id)] = edge
        # 结果条数顶到 LIMIT 就说明还有没取回来的
        return GraphSlice(
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            truncated=len(rows) >= max(1, int(limit)),
        )

    async def search(self, query: str, kb_id: int | None = None, limit: int = 50) -> list[Node]:
        text = (query or "").strip()
        if not text:
            return []
        rows = await self._run(
            f"CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score "
            f"WHERE $kb_id IS NULL OR $kb_id IN coalesce(node.kb_ids, []) "
            f"RETURN node AS n, score ORDER BY score DESC LIMIT $limit",
            {"index": FULLTEXT_INDEX, "q": _lucene_escape(text), "kb_id": kb_id, "limit": int(limit)},
        )
        nodes = [n for n in (_node_from_props(r.get("n")) for r in rows) if n is not None]
        if nodes:
            return nodes
        # 全文索引没命中（或索引还没建好）时退化为子串匹配，保证"搜得到"
        rows = await self._run(
            f"MATCH (n:{NODE_LABEL}) "
            f"WHERE ($kb_id IS NULL OR $kb_id IN coalesce(n.kb_ids, [])) "
            f"  AND (toLower(coalesce(n.name, '')) CONTAINS $q "
            f"       OR toLower(coalesce(n.aliases_text, '')) CONTAINS $q) "
            f"RETURN n ORDER BY size(coalesce(n.name, '')) ASC LIMIT $limit",
            {"q": text.lower(), "kb_id": kb_id, "limit": int(limit)},
        )
        return [n for n in (_node_from_props(r.get("n")) for r in rows) if n is not None]

    async def overview(
        self,
        *,
        entity_types: list[str] | None = None,
        kb_id: int | None = None,
        node_limit: int = 500,
        edge_limit: int = 1500,
    ) -> GraphSlice:
        """全览：取一批节点 + 两端都在结果内的边（给前端画图用）。

        分两条查询而不是一条变长匹配：节点先截断，边再按节点集合过滤，
        这样即使图很大也不会因为「先匹配边」把内存撑爆。
        """
        types = [type_value(t) for t in entity_types] if entity_types else None
        node_rows = await self._run(
            f"MATCH (n:{NODE_LABEL}) "
            f"WHERE ($types IS NULL OR n.entity_type IN $types) "
            f"  AND ($kb_id IS NULL OR $kb_id IN coalesce(n.kb_ids, [])) "
            f"RETURN n LIMIT $limit",
            {"types": types, "kb_id": kb_id, "limit": int(node_limit)},
        )
        nodes = [n for n in (_node_from_props(r.get("n")) for r in node_rows) if n is not None]
        truncated = len(node_rows) >= max(1, int(node_limit))
        if not nodes:
            return GraphSlice(nodes=[], edges=[], truncated=False)

        edge_rows = await self._run(
            f"MATCH (a:{NODE_LABEL})-[r:{REL_TYPE}]->(b:{NODE_LABEL}) "
            f"WHERE a.uid IN $uids AND b.uid IN $uids "
            f"  AND coalesce(r.status, 'auto') <> 'rejected' "
            f"  AND ($kb_id IS NULL OR r.kb_id = $kb_id) "
            f"RETURN a, b, r, startNode(r).uid AS src, endNode(r).uid AS dst LIMIT $limit",
            {"uids": [n.uid for n in nodes], "kb_id": kb_id, "limit": int(edge_limit)},
        )
        if len(edge_rows) >= max(1, int(edge_limit)):
            truncated = True
        slice_ = self._slice(edge_rows, edge_keys=(("r", "src", "dst"),), limit=edge_limit)
        # _slice 会用边里带的节点覆盖，这里以节点查询结果为准（顺序稳定，前端好复用）
        return GraphSlice(nodes=nodes, edges=slice_.edges, truncated=truncated)

    async def stats(self, kb_id: int | None = None) -> dict:
        node_rows = await self._run(
            f"MATCH (n:{NODE_LABEL}) "
            f"WHERE $kb_id IS NULL OR $kb_id IN coalesce(n.kb_ids, []) "
            f"RETURN n.entity_type AS entity_type, count(*) AS c",
            {"kb_id": kb_id},
        )
        edge_rows = await self._run(
            f"MATCH ()-[r:{REL_TYPE}]->() "
            f"WHERE $kb_id IS NULL OR r.kb_id = $kb_id "
            f"RETURN r.relation_type AS rel, count(*) AS c",
            {"kb_id": kb_id},
        )
        by_type = {str(r.get("entity_type") or "unknown"): int(r.get("c") or 0) for r in node_rows}
        by_rel = {str(r.get("rel") or "unknown"): int(r.get("c") or 0) for r in edge_rows}
        return {
            "nodes": {"total": sum(by_type.values()), "by_type": by_type},
            "edges": {"total": sum(by_rel.values()), "by_type": by_rel},
        }

    async def health(self) -> bool:
        try:
            rows = await asyncio.wait_for(
                self._raw("RETURN 1 AS ok"), timeout=self._connect_timeout_s
            )
            return bool(rows and rows[0].get("ok") == 1)
        except Exception as exc:  # noqa: BLE001 - 健康检查失败就是 False
            logger.warning("Neo4j 健康检查失败：%s", exc)
            return False

    async def _raw(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """不做 schema 初始化的裸查询（health 用，避免健康检查触发 DDL）。"""
        async with self._driver().session(database=self._database) as session:
            result = await session.run(cypher, params or {})
            return await result.data()

    async def close(self) -> None:
        if self._driver_obj is not None:
            try:
                await self._driver_obj.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不该影响停机
                logger.warning("关闭 Neo4j 驱动失败：%s", exc)
            finally:
                self._driver_obj = None
                self._schema_ready = False
