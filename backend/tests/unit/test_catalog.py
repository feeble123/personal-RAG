"""G7 知识库元信息：意图识别 + 清单格式化。

用户在问「你拥有哪些知识文件」时必须走文档表，不能走向量检索——
否则会捞回条文片段，模型只能答「参考资料仅覆盖……」。
"""
from __future__ import annotations

from app.services.catalog import format_catalog
from app.services.intent import is_catalog_query


class TestCatalogIntent:
    POSITIVE = [
        "你拥有哪些知识文件？",
        "知识库有哪些文件",
        "有哪些文档？",
        "文件清单",
        "列出所有文档",
        "知识库里收录了哪些规范",
        "系统包含哪些标准",
    ]
    NEGATIVE = [
        "明渠均匀流的形成条件是什么？",
        "GB 50332 中包括哪些规定？",
        "规范中包括哪些内容",
        "这份文件规定了什么？",
        "预警分级有哪几级",
        "设计洪水标准是多少",
    ]

    def test_positive(self):
        for q in self.POSITIVE:
            assert is_catalog_query(q) is True, q

    def test_negative(self):
        for q in self.NEGATIVE:
            assert is_catalog_query(q) is False, q

    def test_empty(self):
        assert is_catalog_query("") is False
        assert is_catalog_query("   ") is False


class TestFormatCatalog:
    def test_kb_mode(self):
        cat = {
            "mode": "kb",
            "kb_name": "水力学",
            "items": [
                {
                    "filename": "水力学.pdf",
                    "file_type": "pdf",
                    "doc_type": "教材",
                    "page_count": 547,
                    "chunk_count": 1204,
                    "status": "ready",
                    "status_zh": "已就绪",
                }
            ],
            "counts": {"total": 1, "ready": 1, "parsing": 0, "failed": 0},
        }
        text = format_catalog(cat)
        assert "水力学" in text
        assert "共收录 1 份文档" in text
        assert "水力学.pdf" in text
        assert "教材" in text
        assert "547 页" in text

    def test_kb_mode_empty(self):
        cat = {
            "mode": "kb",
            "kb_name": "空库",
            "items": [],
            "counts": {"total": 0, "ready": 0, "parsing": 0, "failed": 0},
        }
        assert "暂无文档" in format_catalog(cat)

    def test_all_mode(self):
        cat = {
            "mode": "all",
            "items": [
                {"name": "水力学", "doc_count": 12, "chunk_count": 1445},
                {"name": "防汛预案", "doc_count": 8, "chunk_count": 300},
            ],
            "counts": {"total_kbs": 2},
        }
        text = format_catalog(cat)
        assert "共 2 个知识库" in text
        assert "水力学｜12 份文档" in text
        assert "请先选择该知识库" in text
