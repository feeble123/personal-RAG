"""Chroma 嵌入式向量库封装（HNSW 调优 + metadata 过滤）。

升级路径：当前为本地持久化 Chroma；接口封装为 `build_vector_store()` 工厂，
后续可替换为 Milvus/Qdrant/pgvector（LangChain VectorStore 生态）。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: Any | None = None
_lock = threading.Lock()

_HNSW_CONFIG = {
    "hnsw": {
        "space": settings.hnsw_space,
        "ef_construction": settings.hnsw_ef_construction,
        "max_neighbors": settings.hnsw_max_neighbors,
        "ef_search": settings.hnsw_ef_search,
        "num_threads": 4,
        "batch_size": 100,
    }
}


@dataclass
class SearchHit:
    chunk_id: int
    score: float  # 相似度：cosine distance 转换后（越高越相关）
    distance: float
    metadata: dict[str, Any]


def _get_collection():
    global _client, _collection
    with _lock:
        if _collection is not None:
            return _collection
        _client = chromadb.PersistentClient(path=str(settings.chroma_dir_path))
        name = settings.chroma_collection
        if name not in {c.name for c in _client.list_collections()}:
            try:
                _collection = _client.create_collection(name=name, configuration=_HNSW_CONFIG)
                logger.info("创建 Chroma collection: %s (HNSW=%s)", name, _HNSW_CONFIG["hnsw"])
            except TypeError:
                # 旧版 Chroma：退回默认
                _collection = _client.create_collection(name=name)
        else:
            _collection = _client.get_collection(name)
        return _collection


def reset_collection() -> None:
    """删除并重建整个 collection（清空所有向量）。

    用于重灌后的整库重建：`delete_by_where + add` 反复执行会损坏 HNSW 索引
    （实测 "Error loading hnsw index"，整个集合查询崩溃），重建是最可靠的方式。
    """
    global _collection, _client
    col = _get_collection()
    name = settings.chroma_collection
    try:
        _client.delete_collection(name)
    except Exception:  # 不存在时忽略
        pass
    try:
        _collection = _client.create_collection(name=name, configuration=_HNSW_CONFIG)
    except TypeError:
        _collection = _client.create_collection(name=name)


def _assert_dimension(col, embeddings: list[list[float]]) -> None:
    """P1-3：写入前校验向量维度与 collection 一致（防配置漂移后错配）。"""
    if not embeddings:
        return
    dim = len(embeddings[0])
    # 用 collection 现有第一条向量的维度作参照（空 collection 跳过）
    try:
        existing = col.get(limit=1, include=["embeddings"])
        exist_emb = existing.get("embeddings")
        # numpy 数组不能用 `or []`（truth value 歧义），用显式长度判断
        if exist_emb is not None and len(exist_emb) > 0:
            if len(exist_emb[0]) != dim:
                raise ValueError(
                    f"向量维度不匹配: 写入 {dim} 维 vs collection 已有 {len(exist_emb[0])} 维"
                )
    except ValueError:
        raise
    except Exception:
        pass  # 读参照失败（新 collection 等）跳过


def add_vectors(
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    col = _get_collection()
    _assert_dimension(col, embeddings)
    col.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)


# ---- P0-8 影子索引：先建 shadow → 核对 → 原子改名切换（失败旧 collection 原样可查）----
_SHADOW_SUFFIX = "_shadow"


def _shadow_name() -> str:
    return f"{settings.chroma_collection}{_SHADOW_SUFFIX}"


def _client_instance() -> chromadb.ClientAPI:
    """确保 _client 初始化并返回。与 _get_collection 复用同一把 _lock 的初始化路径。

    注意：不能在本函数里用 `with _lock` 包住 `_get_collection()`——后者自身也
    `with _lock`，threading.Lock 非重入 → 同一线程二次 acquire 死锁（实测首次
    build_shadow 卡死）。改为直接调 _get_collection() 完成初始化。
    """
    _get_collection()  # 初始化 _client + _collection
    return _client


def build_shadow(
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> int:
    """建影子 collection 并写入全量向量，返回实际 count。

    不碰 active collection——任何写入失败，active 原样保留可查。
    """
    client = _client_instance()
    shadow = _shadow_name()
    try:
        client.delete_collection(shadow)
    except Exception:  # 不存在时忽略
        pass
    try:
        col = client.create_collection(name=shadow, configuration=_HNSW_CONFIG)
    except TypeError:
        col = client.create_collection(name=shadow)
    # P1-3：维度校验（对照 active collection 现有向量，防错配）
    active = _get_collection()
    try:
        existing = active.get(limit=1, include=["embeddings"])
        exist_emb = existing.get("embeddings")
        if (
            exist_emb is not None
            and len(exist_emb) > 0
            and embeddings
            and len(exist_emb[0]) != len(embeddings[0])
        ):
            raise ValueError(
                f"影子索引维度不匹配: 写入 {len(embeddings[0])} 维 vs active 已有 {len(exist_emb[0])} 维"
            )
    except ValueError:
        raise
    except Exception:
        pass
    col.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    return col.count()


def swap_shadow_to_active() -> None:
    """原子切换：删 active → shadow 改名 active。

    rename 失败窗口（删 live → 改名前）内查询可能空；失败则回退 reset_collection，
    由调用方在 try/except 里做兜底重建（见 manager._rebuild_chroma）。
    """
    global _client, _collection
    client = _client_instance()
    shadow = _shadow_name()
    name = settings.chroma_collection
    try:
        client.delete_collection(name)
    except Exception:  # 不存在时忽略
        pass
    client.get_collection(shadow).modify(name=name)
    _collection = client.get_collection(name)


def drop_shadow() -> None:
    """清理影子 collection（发布失败/中止时）。"""
    global _client
    client = _client_instance()
    shadow = _shadow_name()
    try:
        client.delete_collection(shadow)
    except Exception:  # 不存在时忽略
        pass


def gc_orphan_hnsw_dirs() -> int:
    """清理 Chroma 残留的孤儿 HNSW 索引目录（治本：防重灌累积垃圾）。

    背景：`delete_collection` 只删 chroma.sqlite3 里的登记记录，不删磁盘上的
    旧 HNSW 索引目录，每次重灌都留下 ~10MB 孤儿，越攒越多（实测一度 22 个 211MB）。

    挂载点（重要）：在**应用启动时**调用（lifespan），而非 `delete_collection` 之后。
    因为 Windows 上 Chroma 删除 collection 后 HNSW 文件句柄不会立即释放，紧跟着
    move 旧目录会撞 `WinError 32`（实测）。启动时不存在刚发生的 delete，历史孤儿
    的句柄早已释放，move 才安全。

    安全策略（绝不碰 active 索引）：
    1. 以 chroma.sqlite3 的 segments 表为唯一权威，取出当前 VECTOR scope 的
       segment id（active 索引目录名 == segment id）。
    2. 只把「磁盘上存在、但 segments 表里没有」的 HNSW 目录**移到**回收区
       （`<chroma_dir>/../_chroma_gc_backup/`），而非 rm——可恢复。
    3. active 目录一律跳过。

    返回移走的目录数量（0 表示无残留）。
    """
    chroma_dir = Path(settings.chroma_dir_path)
    if not chroma_dir.is_dir():
        return 0

    # 1) 从登记簿取 active VECTOR segment id（可能为空，例如刚初始化无索引）
    sqlite_path = chroma_dir / "chroma.sqlite3"
    if not sqlite_path.is_file():
        return 0  # 尚无登记簿（新库/测试临时目录），无残留可清
    active_ids: set[str] = set()
    try:
        con = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT id FROM segments WHERE scope = 'VECTOR'")
            active_ids = {row[0] for row in cur.fetchall()}
        finally:
            con.close()
    except Exception as exc:  # 登记簿不可读时不做任何删除，安全失败
        logger.warning("GC 读取 chroma.sqlite3 失败，跳过残留清理: %s", exc)
        return 0

    # 2) 扫描 HNSW 目录（特征文件 data_level0.bin），分类孤儿
    backup_root = chroma_dir.parent / "_chroma_gc_backup"
    moved = 0
    for entry in sorted(chroma_dir.iterdir()):
        if not entry.is_dir():
            continue
        # 只认 HNSW 索引目录：必须含 data_level0.bin，且目录名是 UUID
        if not (entry / "data_level0.bin").is_file():
            continue
        if entry.name in active_ids:
            continue  # active 索引，绝不碰
        # 孤儿：移到回收区（跨盘可能 fallback 到复制+删除）
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(backup_root / entry.name))
            moved += 1
            logger.info("GC 移走孤儿 HNSW 目录: %s", entry.name)
        except Exception as exc:
            logger.warning("GC 移走 %s 失败，保留原样: %s", entry.name, exc)

    return moved


def upsert_vectors(
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    col = _get_collection()
    col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)


def delete_by_where(where: dict[str, Any]) -> None:
    col = _get_collection()
    try:
        col.delete(where=where)
    except Exception as exc:  # 不存在时删除可能报错，容忍
        logger.debug("Chroma delete(%s) 忽略异常: %s", where, exc)


def query(
    query_vector: list[float],
    where: dict[str, Any] | None = None,
    n_results: int = 10,
) -> list[SearchHit]:
    col = _get_collection()
    res = col.query(
        query_embeddings=[query_vector],
        where=where,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    hits: list[SearchHit] = []
    ids = res.get("ids", [[]])[0]
    distances = res.get("distances", [[]])[0]
    metadatas = res.get("metadatas", [[]])[0]
    for cid, dist, meta in zip(ids, distances, metadatas):
        try:
            chunk_id = int(cid)
        except (TypeError, ValueError):
            continue
        # cosine distance → 相似度（越小距离越相关）
        similarity = max(0.0, 1.0 - float(dist))
        hits.append(SearchHit(chunk_id=chunk_id, score=similarity, distance=float(dist), metadata=meta or {}))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def count() -> int:
    return _get_collection().count()


def get_embeddings_by_ids(ids: list[int]) -> dict[int, list[float]]:
    """按 chunk id 取向量（用于 BM25 补召回 chunk 的实时余弦计算）。"""
    if not ids:
        return {}
    col = _get_collection()
    res = col.get(ids=[str(i) for i in ids], include=["embeddings"])
    out: dict[int, list[float]] = {}
    for cid, emb in zip(res.get("ids", []), res.get("embeddings", [])):
        try:
            out[int(cid)] = emb
        except (TypeError, ValueError):
            continue
    return out
