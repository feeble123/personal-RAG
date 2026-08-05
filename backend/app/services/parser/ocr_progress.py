"""入库进度追踪（进程内，键=存储文件名）。

- OCR 是解析中最耗时的阶段（扫描件可达数分钟），逐页更新进度；
- 键用 doc.stored_path（上传时的 uuid 文件名，唯一），解析完成即清除；
- 升级路径：未来多进程/分布式部署时，可替换为 Redis 或 DB 实现，接口不变。
"""
from __future__ import annotations

_entries: dict[str, dict] = {}


def set_progress(key: str, **fields) -> None:
    """更新某个文档的进度字段（stage/done/total 等）。"""
    entry = _entries.setdefault(key, {})
    entry.update(fields)


def get_progress(key: str) -> dict | None:
    return _entries.get(key)


def clear_progress(key: str) -> None:
    _entries.pop(key, None)
