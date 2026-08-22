"""P1-1 单元2：IR snapshot 固定。

代表文档的 IR JSON 与固定快照对比——任何 parser 结构变化须人工审核差异。
快照存 tests/fixtures/ir_snapshots/*.json（首次生成需 --update-snapshots 写盘）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.parser.factory import get_parser

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ir_snapshots"


def _ir_dict(parsed) -> dict:
    """把 ParsedDocument 序列化为可对比结构（元素列表 + 质量关键字段）。"""
    return {
        "parser": parsed.quality.get("parser"),
        "element_count": len(parsed.elements),
        "elements": [el.to_dict() for el in parsed.elements],
    }


@pytest.fixture
def sample_docs(tmp_path):
    """构造代表文档：md 层级 + txt 段落 + csv 表格。"""
    md = tmp_path / "sample.md"
    md.write_text(
        "# 第一章 总则\n\n本规范规定了水利工程的设计要求。\n\n"
        "## 1.1 基本规定\n\n设计应符合国家现行标准。\n\n"
        "## 1.2 术语\n\n本规范采用下列术语。\n",
        encoding="utf-8",
    )
    txt = tmp_path / "sample.txt"
    txt.write_text("第一段文本内容。\n\n第二段文本内容。\n", encoding="utf-8")
    csv = tmp_path / "sample.csv"
    csv.write_text("名称,数量\n甲,1\n乙,2\n", encoding="utf-8-sig")
    return {"md": (md, "sample.md"), "txt": (txt, "sample.txt"), "csv": (csv, "sample.csv")}


def _snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"{name}.json"


class TestIRSnapshot:
    def test_md_snapshot(self, sample_docs, tmp_path):
        path, fname = sample_docs["md"]
        parsed = get_parser(fname).parse(path, fname)
        got = _ir_dict(parsed)
        sp = _snapshot_path("md")
        if not sp.exists():
            # 首次：写盘后由人工审核
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(got, ensure_ascii=False, indent=2), encoding="utf-8")
            pytest.skip(f"snapshot 首次生成: {sp}")
        expected = json.loads(sp.read_text(encoding="utf-8"))
        assert got == expected, f"md IR snapshot 变化，请人工审核差异:\n{sp}"

    def test_txt_snapshot(self, sample_docs, tmp_path):
        path, fname = sample_docs["txt"]
        parsed = get_parser(fname).parse(path, fname)
        got = _ir_dict(parsed)
        sp = _snapshot_path("txt")
        if not sp.exists():
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(got, ensure_ascii=False, indent=2), encoding="utf-8")
            pytest.skip(f"snapshot 首次生成: {sp}")
        expected = json.loads(sp.read_text(encoding="utf-8"))
        assert got == expected, f"txt IR snapshot 变化，请人工审核差异:\n{sp}"

    def test_csv_snapshot(self, sample_docs, tmp_path):
        path, fname = sample_docs["csv"]
        parsed = get_parser(fname).parse(path, fname)
        got = _ir_dict(parsed)
        sp = _snapshot_path("csv")
        if not sp.exists():
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(got, ensure_ascii=False, indent=2), encoding="utf-8")
            pytest.skip(f"snapshot 首次生成: {sp}")
        expected = json.loads(sp.read_text(encoding="utf-8"))
        assert got == expected, f"csv IR snapshot 变化，请人工审核差异:\n{sp}"
