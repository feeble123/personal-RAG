"""水印/广告噪声过滤单元测试：跨页重复行 + LLM 兜底。"""
from __future__ import annotations

from app.services.parser.base import ParsedBlock
from app.services.parser.boilerplate import (
    collect_ad_candidates,
    filter_repeated_lines,
    filter_text_lines,
    llm_filter_ads,
    remove_lines,
)


def _block(text: str, page: int, btype: str = "paragraph") -> ParsedBlock:
    return ParsedBlock(text=text, section=None, page=page, block_type=btype)


class TestFilterRepeatedLines:
    @staticmethod
    def _watermark_blocks(pages):
        """每页 4 行水印 + 1 行正文的块。"""
        blocks = []
        for p in pages:
            blocks.append(_block(f"正文内容第{p}页", p))
            blocks.append(_block("引用于《某规范》2023年第一版某出版社", p))
            blocks.append(_block("本资料限内部使用，严禁用于商业。", p))
            blocks.append(_block("钢管购买热线：13337883086(微信同号)", p))
            blocks.append(_block("https://emlog.josen.net", p))
        return blocks

    def test_watermark_on_many_pages_removed(self):
        blocks = self._watermark_blocks(range(1, 11))
        out, repeated, removed = filter_repeated_lines(blocks, 10)
        assert "钢管购买热线：13337883086(微信同号)" in removed
        assert "https://emlog.josen.net" in removed
        assert any("正文内容第3页" in b.text for b in out)
        assert not any("钢管购买热线" in b.text for b in out)
        assert not any("https://emlog" in b.text for b in out)

    def test_rare_line_kept(self):
        """只在少数页出现的行（正文）不被当水印移除。"""
        blocks = [_block("唯一出现的一行内容", 1), _block("另一行", 2), _block("再一行", 3)]
        out, _, removed = filter_repeated_lines(blocks, 10)
        assert removed == []
        assert len(out) == 3

    def test_emptied_block_deleted(self):
        """块被滤空（只剩水印）→ 删除该块。"""
        blocks = [
            _block("钢管购买热线：13337883086(微信同号)", 1),
            _block("钢管购买热线：13337883086(微信同号)", 2),
            _block("钢管购买热线：13337883086(微信同号)", 3),
            _block("正文", 4),
        ]
        out, _, removed = filter_repeated_lines(blocks, 4)
        assert "钢管购买热线：13337883086(微信同号)" in removed
        assert len(out) == 1  # 3 个水印块被删，只剩正文

    def test_heading_kept_when_emptied(self):
        """标题块被滤空（罕见）→ 保留原样避免丢标题。"""
        blocks = [
            _block("钢管购买热线：13337883086(微信同号)", 1, btype="heading"),
            _block("钢管购买热线：13337883086(微信同号)", 2, btype="heading"),
            _block("钢管购买热线：13337883086(微信同号)", 3, btype="heading"),
            _block("正文", 4),
        ]
        out, _, _ = filter_repeated_lines(blocks, 4)
        assert any(b.block_type == "heading" and b.text for b in out)


class TestFilterTextLines:
    def test_removes_repeated_lines(self):
        text = "引用于《某规范》2023年第一版\n1 总则........... 1\n本资料限内部使用\n2 主要符号"
        out = filter_text_lines(text, {"引用于《某规范》2023年第一版", "本资料限内部使用"})
        assert "引用于《某规范》" not in out
        assert "本资料限内部使用" not in out
        assert "1 总则" in out and "2 主要符号" in out


class TestCollectAdCandidates:
    def test_finds_ad_feature_lines(self):
        blocks = [
            _block("技术条款内容", 1),
            _block("https://ad.example.com", 2),
            _block("咨询热线：400-123", 3),
        ]
        cands = collect_ad_candidates(blocks)
        assert "https://ad.example.com" in cands
        assert "咨询热线：400-123" in cands
        assert "技术条款内容" not in cands


class TestLlmFilterAds:
    async def test_returns_flagged_lines(self, monkeypatch):
        class FakeLLM:
            async def ainvoke(self, prompt):
                return type("R", (), {"content": "[0, 2]"})()
        monkeypatch.setattr("app.services.chat.build_chat_model", lambda t: FakeLLM())
        out = await llm_filter_ads(["https://ad.com", "技术内容", "内部资料"])
        assert out == ["https://ad.com", "内部资料"]

    async def test_degrades_on_error(self):
        """fake 无 ainvoke / 异常 → 返回 []（不阻塞入库）。"""
        out = await llm_filter_ads(["https://ad.com"])
        assert out == []


class TestRemoveLines:
    def test_removes_specified(self):
        blocks = [_block("技术内容\n内部资料，仅供学习交流", 1)]
        out = remove_lines(blocks, {"内部资料，仅供学习交流"})
        assert len(out) == 1
        assert "内部资料" not in out[0].text
        assert "技术内容" in out[0].text
