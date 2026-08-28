"""单元 D：gc_orphan_hnsw_dirs 残留清理回归测试。

背景：Chroma `delete_collection` 只删 chroma.sqlite3 登记记录，不删磁盘上的旧
HNSW 索引目录，重灌越攒越多。gc_orphan_hnsw_dirs 负责把「磁盘上有、登记簿里无」
的孤儿目录移到回收区，绝不碰 active 索引。

本测试锁定三条安全底线：
1. active 索引目录（segments 表里的 VECTOR id）绝不被移动；
2. 孤儿 HNSW 目录被移到回收区（可恢复，不是删除）；
3. 清理后 collection count 不变、查询正常。
"""
from __future__ import annotations

import sqlite3

import chromadb

from app.services import vector_store


def _active_vector_ids(chroma_dir) -> set[str]:
    con = sqlite3.connect(f"file:{(chroma_dir / 'chroma.sqlite3').as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM segments WHERE scope = 'VECTOR'")
        return {row[0] for row in cur.fetchall()}
    finally:
        con.close()


def _make_chroma_with_collection(chroma_dir) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(chroma_dir))
    col = client.create_collection("kb_chunks")
    col.add(
        ids=["1", "2", "3"],
        embeddings=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]],
        documents=["明渠均匀流", "水静力学", "水头损失"],
    )
    return col


class TestGcOrphanHnswDirs:
    def test_moves_orphan_keeps_active(self, monkeypatch, tmp_path):
        chroma_dir = tmp_path / "chroma"
        col = _make_chroma_with_collection(chroma_dir)
        active_ids = _active_vector_ids(chroma_dir)
        assert len(active_ids) >= 1

        # 手工造一个孤儿 HNSW 目录（含特征文件 data_level0.bin）
        orphan = chroma_dir / "deadbeef-0000-0000-0000-000000000000"
        orphan.mkdir()
        (orphan / "data_level0.bin").write_bytes(b"x" * 64)

        monkeypatch.setattr(vector_store.settings, "chroma_dir", str(chroma_dir))
        moved = vector_store.gc_orphan_hnsw_dirs()

        assert moved == 1
        assert not orphan.exists(), "孤儿目录应被移走"
        assert (tmp_path / "_chroma_gc_backup" / orphan.name).exists(), "孤儿应在回收区（可恢复）"
        # active 目录一个不少地保留
        for aid in active_ids:
            assert (chroma_dir / aid).is_dir(), f"active 索引 {aid} 不应被移走"
        # 清理后 count 不变
        assert col.count() == 3

    def test_no_orphan_is_noop(self, monkeypatch, tmp_path):
        chroma_dir = tmp_path / "chroma"
        _make_chroma_with_collection(chroma_dir)
        active_ids = _active_vector_ids(chroma_dir)

        monkeypatch.setattr(vector_store.settings, "chroma_dir", str(chroma_dir))
        moved = vector_store.gc_orphan_hnsw_dirs()

        assert moved == 0
        for aid in active_ids:
            assert (chroma_dir / aid).is_dir()

    def test_missing_chroma_dir_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vector_store.settings, "chroma_dir", str(tmp_path / "nope"))
        assert vector_store.gc_orphan_hnsw_dirs() == 0

    def test_ignores_non_hnsw_dirs(self, monkeypatch, tmp_path):
        """非 HNSW 目录（无 data_level0.bin）不参与判断，不会被误删。"""
        chroma_dir = tmp_path / "chroma"
        _make_chroma_with_collection(chroma_dir)
        # 造一个无 data_level0.bin 的普通目录（模拟非索引目录）
        other = chroma_dir / "some-other-dir"
        other.mkdir()
        (other / "readme.txt").write_text("not an index")

        monkeypatch.setattr(vector_store.settings, "chroma_dir", str(chroma_dir))
        moved = vector_store.gc_orphan_hnsw_dirs()

        assert moved == 0
        assert other.is_dir(), "非 HNSW 目录不应被移动"
