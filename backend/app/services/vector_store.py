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
from chromadb.errors import NotFoundError

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: Any | None = None
_collections: dict[str, Any] = {}   # B1：按名字缓存 collection（软切换原语用）
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
    global _collection
    _collection = _get_collection_by_name(settings.chroma_collection)
    return _collection


# ---- B1：按名字操作 collection（软切换原语，为「每库每版本一个 collection」铺路）----

def _get_collection_by_name(name: str) -> Any:
    """按名字取（懒建）collection，进程内缓存，线程安全。

    与 _get_collection 的区别：后者固定返回默认 collection（kb_chunks）；
    这里支持任意名字（软切换时每库每版本一个独立 collection，互不牵连）。
    """
    global _client
    with _lock:
        col = _collections.get(name)
        if col is not None:
            return col
        if _client is None:
            _client = chromadb.PersistentClient(path=str(settings.chroma_dir_path))
        if name not in {c.name for c in _client.list_collections()}:
            try:
                col = _client.create_collection(name=name, configuration=_HNSW_CONFIG)
                logger.info("创建 Chroma collection: %s (HNSW=%s)", name, _HNSW_CONFIG["hnsw"])
            except TypeError:
                # 旧版 Chroma：退回默认
                col = _client.create_collection(name=name)
        else:
            col = _client.get_collection(name)
        _collections[name] = col
        return col


def _drop_collection_named(name: str) -> None:
    """物理删除指定名字的 collection + 失效进程内缓存（不存在时忽略）。"""
    global _client
    with _lock:
        if _client is None:
            _client = chromadb.PersistentClient(path=str(settings.chroma_dir_path))
        try:
            _client.delete_collection(name)
        except NotFoundError:  # 不存在时忽略（其他异常如磁盘满/权限错不吞，向上抛）
            pass
        _collections.pop(name, None)


def build_collection(
    name: str,
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> int:
    """建（或重建）指定名字的 collection 并写入全量向量，返回实际 count。

    不碰其他 collection——「每库每版本一个 collection」的软切换第一步：
    先建新版本 collection，核对 count 一致后再切指针。同名残留先清（幂等）。
    """
    _drop_collection_named(name)
    col = _get_collection_by_name(name)
    _assert_dimension(col, embeddings)
    if ids:
        col.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    return col.count()


def drop_collection(name: str) -> None:
    """删除指定名字的 collection（软切换后清理旧版本用）。"""
    _drop_collection_named(name)


def drop_collections_by_prefix(prefix: str) -> list[str]:
    """删除所有名字以 prefix 开头的 collection（整库删除时清该库全部版本）。

    返回被删除的 collection 名列表。软切换下每个库有多个版本号 collection，
    删库时需一并清掉；此函数不碰其他库（前缀即库隔离）。
    """
    global _client
    client = _client_instance()
    removed: list[str] = []
    for name in [c.name for c in client.list_collections()]:
        if name.startswith(prefix):
            _drop_collection_named(name)
            removed.append(name)
    return removed


def query_named(
    name: str,
    query_vector: list[float],
    where: dict[str, Any] | None = None,
    n_results: int = 10,
) -> list[SearchHit]:
    """在指定名字的 collection 里检索（软切换后按 active 版本 collection 名查）。"""
    return _query_collection(_get_collection_by_name(name), query_vector, where, n_results)


def query_many(
    names: list[str],
    query_vector: list[float],
    where: dict[str, Any] | None = None,
    n_results: int = 10,
) -> list[SearchHit]:
    """在多个 collection 里检索并合并（跨库 kb_id=None 时用）。

    每个 collection 各查 n_results，合并后按相似度降序截断到 n_results。
    where 过滤按 collection 各自应用（metadata 层隔离，跨库结果互不污染）。
    """
    all_hits: list[SearchHit] = []
    for name in names:
        try:
            all_hits.extend(
                _query_collection(_get_collection_by_name(name), query_vector, where, n_results)
            )
        except Exception as exc:
            logger.warning("查询 collection %s 失败，跳过: %s", name, exc)
            continue
    all_hits.sort(key=lambda h: h.score, reverse=True)
    return all_hits[:n_results]


def count_named(name: str) -> int:
    return _get_collection_by_name(name).count()


def delete_by_where_named(name: str, where: dict[str, Any]) -> None:
    col = _get_collection_by_name(name)
    try:
        col.delete(where=where)
    except Exception as exc:  # 不存在时删除可能报错，容忍
        logger.debug("Chroma delete(%s) 忽略异常: %s", where, exc)


def get_embeddings_by_ids_named(name: str, ids: list[int]) -> dict[int, list[float]]:
    """在指定名字的 collection 里按 chunk id 取向量。"""
    return _get_embeddings(_get_collection_by_name(name), ids)


def get_embeddings_by_ids_many(names: list[str], ids: list[int]) -> dict[int, list[float]]:
    """跨多个 collection 取向量并合并（补召回 chunk 可能跨库）。"""
    out: dict[int, list[float]] = {}
    for name in names:
        try:
            out.update(_get_embeddings(_get_collection_by_name(name), ids))
        except Exception as exc:
            logger.warning("取向量 collection %s 失败，跳过: %s", name, exc)
            continue
    return out


def _query_collection(
    col: Any,
    query_vector: list[float],
    where: dict[str, Any] | None,
    n_results: int,
) -> list[SearchHit]:
    """把 Chroma query 结果转成 SearchHit 列表（query / query_named 共用）。"""
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


def _get_embeddings(col: Any, ids: list[int]) -> dict[int, list[float]]:
    """按 chunk id 取向量（get_embeddings_by_ids / get_embeddings_by_ids_named 共用）。"""
    if not ids:
        return {}
    res = col.get(ids=[str(i) for i in ids], include=["embeddings"])
    out: dict[int, list[float]] = {}
    for cid, emb in zip(res.get("ids", []), res.get("embeddings", [])):
        try:
            out[int(cid)] = emb
        except (TypeError, ValueError):
            continue
    return out


def reset_collection() -> None:
    """删除并重建当前 active collection（清空所有向量）。

    用于重灌后的整库重建：`delete_by_where + add` 反复执行会损坏 HNSW 索引
    （实测 "Error loading hnsw index"，整个集合查询崩溃），重建是最可靠的方式。
    """
    global _collection
    name = settings.chroma_collection
    _drop_collection_named(name)
    _collection = _get_collection_by_name(name)


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
    except NotFoundError:
        pass  # 新 collection 无向量，跳过维度校验（正常路径）
    except Exception as exc:
        logger.warning("维度校验读参照失败，跳过: %s", exc)


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
    except NotFoundError:  # 不存在时忽略（其他异常如磁盘满/权限错不吞，向上抛）
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
    except NotFoundError:
        pass  # active collection 无向量，跳过维度校验（正常路径）
    except Exception as exc:
        logger.warning("影子维度校验读参照失败，跳过: %s", exc)
    col.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    return col.count()


def swap_shadow_to_active() -> None:
    """原子切换：删 active → shadow 改名 active。

    rename 失败窗口（删 live → 改名前）内查询可能空；失败则回退 reset_collection，
    由调用方在 try/except 里做兜底重建（见 manager._rebuild_chroma）。

    遗留兼容层：B2 起生产发布改用 build_collection + IndexVersion 指针软切换，
    此函数仅测试/旧脚本使用。删除/改名后同步失效 _collections 缓存，避免缓存
    持有已删 collection 的悬空 handle。
    """
    global _client, _collection
    client = _client_instance()
    shadow = _shadow_name()
    name = settings.chroma_collection
    with _lock:
        try:
            client.delete_collection(name)
        except NotFoundError:  # 不存在时忽略（其他异常如磁盘满/权限错不吞，向上抛）
            pass
        _collections.pop(name, None)  # 失效旧 active 缓存
        client.get_collection(shadow).modify(name=name)
        _collections.pop(shadow, None)  # shadow 改名后旧缓存失效
    _collection = _get_collection_by_name(name)  # 锁外预热（_get_collection_by_name 自身加锁）


def drop_shadow() -> None:
    """清理影子 collection（发布失败/中止时）。"""
    global _client
    client = _client_instance()
    shadow = _shadow_name()
    try:
        client.delete_collection(shadow)
    except NotFoundError:  # 不存在时忽略（其他异常如磁盘满/权限错不吞，向上抛）
        pass
    with _lock:
        _collections.pop(shadow, None)


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
    return _query_collection(_get_collection(), query_vector, where, n_results)


def count() -> int:
    return _get_collection().count()


def count_all() -> int:
    """统计所有 kb_chunks* collection 的向量总数（软切换后每库一个 collection）。

    健康探活/统计展示用：跨所有库的 active+retired collection 求和。
    """
    client = _client_instance()
    total = 0
    for name in [c.name for c in client.list_collections()]:
        if name.startswith(settings.chroma_collection):
            try:
                total += _get_collection_by_name(name).count()
            except Exception as exc:
                logger.warning("count collection %s 失败，跳过: %s", name, exc)
    return total


def get_embeddings_by_ids(ids: list[int]) -> dict[int, list[float]]:
    """按 chunk id 取向量（用于 BM25 补召回 chunk 的实时余弦计算）。"""
    return _get_embeddings(_get_collection(), ids)
