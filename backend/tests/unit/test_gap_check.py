"""LLM 断号补全单元测试：编号扫描 / 候选算法 / LLM 确认（裁判）/ 离线降级。"""
from __future__ import annotations

import pytest

from app.services.parser import gap_check
from app.services.parser.base import ParsedBlock
from app.services.parser.gap_check import FoundNumber
from app.services.parser.toc import TocEntry, TocInfo


def _found(number, page, block_index=0, line_index=0, text=None):
    return FoundNumber(number=number, text=text or number, page=page, block_index=block_index, line_index=line_index)


class TestScanNumberedLines:
    def test_scan_captures_line_start_numbers(self):
        blocks = [
            ParsedBlock(text="1 总则\n总则内容", page=4, block_type="paragraph"),
            ParsedBlock(text="1.2.3 三级条款\n内容", page=5, block_type="paragraph"),
        ]
        found = gap_check.scan_numbered_lines(blocks)
        assert [(f.number, f.page, f.block_index, f.line_index) for f in found] == [
            ("1", 4, 0, 0),
            ("1.2.3", 5, 1, 0),
        ]

    def test_scan_skips_dates(self):
        blocks = [ParsedBlock(text="2020.10.01 生效\n正文", page=1, block_type="paragraph")]
        found = gap_check.scan_numbered_lines(blocks)
        assert found == []


class TestCandidateMissing:
    def test_sibling_gap_with_both_neighbors(self):
        found = [_found("1.1", 1), _found("1.3", 2)]
        assert gap_check.candidate_missing(None, found) == ["1.2"]

    def test_sibling_gap_requires_neighbors(self):
        # 1.1、1.5：中间缺 1.2/1.3/1.4，但各缺号的上下邻居不全 → 不候选（防凭空猜）
        found = [_found("1.1", 1), _found("1.5", 5)]
        assert gap_check.candidate_missing(None, found) == []

    def test_toc_missing_entry_is_candidate(self):
        toc = TocInfo(
            entries=[TocEntry(number="1.2", title="适用范围", printed_page=2, level=2)],
            toc_pages=[2], offset=3, source="text",
        )
        found = [_found("1", 4), _found("1.1", 5)]
        cands = gap_check.candidate_missing(toc, found)
        assert "1.2" in cands

    def test_no_candidates_when_complete(self):
        found = [_found("1.1", 1), _found("1.2", 2), _found("1.3", 3)]
        assert gap_check.candidate_missing(None, found) == []


class _FakeLLM:
    """测试用：带 ainvoke 的假 LLM，返回固定 JSON。"""

    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages):  # noqa: ANN001
        return type("Resp", (), {"content": self._content})


class TestConfirmMissing:
    async def test_llm_confirms_from_candidates_only(self, monkeypatch):
        """LLM 只能从候选里筛：返回候选外的 99.9 被过滤掉。"""
        monkeypatch.setattr(
            gap_check, "build_chat_model",
            lambda temp: _FakeLLM('{"missing": ["1.2", "99.9"]}'),
        )
        found = [_found("1.1", 1), _found("1.3", 2)]
        confirmed = await gap_check.confirm_missing(None, found)
        assert confirmed == {"1.2"}

    async def test_llm_error_degrades_to_none(self, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("LLM 不可用")

        monkeypatch.setattr(gap_check, "build_chat_model", lambda temp: type("X", (), {"ainvoke": boom})())
        found = [_found("1.1", 1), _found("1.3", 2)]
        assert await gap_check.confirm_missing(None, found) is None

    async def test_fake_llm_no_ainvoke_degrades(self):
        """离线 FAKE LLM 无 ainvoke → 不崩、返回 None（调用方降级为全候选）。"""
        found = [_found("1.1", 1), _found("1.3", 2)]
        assert await gap_check.confirm_missing(None, found) is None

    async def test_no_candidates_returns_empty(self):
        found = [_found("1.1", 1), _found("1.2", 2)]
        assert await gap_check.confirm_missing(None, found) == set()
