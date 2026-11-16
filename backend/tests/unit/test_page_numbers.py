"""书内印刷页码：用户拿着书翻，引用里的页码必须对得上书上印的。

我们存的 `Chunk.page` 是 PDF 物理页序——书里的「157」在 PDF 里是第 171 页，
差的 14 页是封面/版权/前言/目录。MinerU 本来就逐页认出了印刷页码，
这里守的是「能不能可信地把它捞回来」。
"""
from __future__ import annotations

from app.services.parser.mineru import build_page_number_map


def _items(pairs: list[tuple[int, str]]) -> list[dict]:
    """pairs: (page_idx(0起), 页码文本)"""
    return [{"type": "page_number", "page_idx": i, "text": t} for i, t in pairs]


class TestBuildPageNumberMap:
    def test_偏移一致的页码被采纳(self):
        # 物理页 = page_idx+1；印刷页 = 物理页 - 14
        items = _items([(14, "1"), (15, "2"), (16, "3"), (170, "157")])
        m = build_page_number_map(items)
        assert m[15] == 1       # page_idx 14 → 物理页 15
        assert m[171] == 157    # page_idx 170 → 物理页 171

    def test_版权行被当成页码时丢弃(self):
        """实测 MinerU 把「版权所有·侵权必究」标成了 page_number。"""
        items = _items([(3, "版权所有·侵权必究"), (14, "1"), (15, "2"), (16, "3")])
        m = build_page_number_map(items)
        assert 4 not in m
        assert m[15] == 1

    def test_偏移不一致的页整页丢弃(self):
        """附录另编页码时，那种页宁可没有，也不给一个错的。"""
        items = _items(
            [(14, "1"), (15, "2"), (16, "3"), (17, "4"), (18, "5"),
             (300, "A-1")]  # A-1 会被正则挡掉
        )
        m = build_page_number_map(items)
        assert m[15] == 1
        assert 301 not in m

    def test_样本太少就不猜(self):
        assert build_page_number_map(_items([(14, "1"), (15, "2")])) == {}

    def test_没有页码元素返回空(self):
        assert build_page_number_map([{"type": "text", "page_idx": 0, "text": "正文"}]) == {}

    def test_带装饰的页码能认出来(self):
        items = _items([(14, "· 1 ·"), (15, "— 2 —"), (16, "- 3 -")])
        assert build_page_number_map(items)[15] == 1

    def test_纯文本里夹数字不算页码(self):
        items = _items([(14, "1998 年第一版"), (15, "2"), (16, "3"), (17, "4")])
        assert 15 not in build_page_number_map(items)
