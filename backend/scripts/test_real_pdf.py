"""测试真实 PDF 解析：直接调用分层解析器输出质量指标与分块结果。

用法：python scripts/test_real_pdf.py "<pdf路径>"
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 确保可导入 app
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/test_real_pdf.py <pdf路径> [old|new]  # 第二个参数为切片策略")
        return
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"文件不存在: {p}")
        return
    strategy = sys.argv[2] if len(sys.argv) > 2 else "old"
    print(f"文件: {p.name} | 大小: {p.stat().st_size/1024:.1f} KB | 切片策略: {strategy}")

    from app.services.chunker import chunk_blocks
    from app.services.parser.factory import get_parser

    t0 = time.time()
    parsed = get_parser(p.name).parse(p, p.name, strategy)
    print(f"解析耗时: {time.time()-t0:.2f}s | 页数: {parsed.page_count} | 块数: {len(parsed.blocks)}")
    print("质量指标:", parsed.quality)

    # ---- 目录（TOC）权威大纲 + 断号注入验证 ----
    toc = parsed.outline
    if toc is None:
        print(">> 未识别到目录（outline=None），注入跳过（行为=现状）。")
    else:
        print(f"\n>> 目录大纲: entries={len(toc.entries)} offset={toc.offset} source={toc.source} "
              f"toc_pages={toc.toc_pages}")
        for e in toc.entries[:18]:
            print(f"   {e.number:<10} {e.title[:30]:<32} 印页={e.printed_page} 物理页={e.physical_page} L{e.level}")

        from app.services.parser import gap_check, outline as outline_mod

        found = gap_check.scan_numbered_lines(parsed.blocks)
        confirmed = set(gap_check.candidate_missing(toc, found))
        print(f"\n扫描行首编号 {len(found)} 个；算法候选缺失 {len(confirmed)} 个: {sorted(confirmed)[:25]}")
        injected = outline_mod.inject_blocks(parsed.blocks, toc, confirmed, found)
        print(f"注入后 blocks {len(parsed.blocks)} -> {len(injected)}（净增 {len(injected)-len(parsed.blocks)}）")
        soft_n = sum(1 for b in injected if b.block_type == "soft_heading")
        print(f"  软标题数={soft_n}；注入样例行：")
        shown = 0
        for b in injected:
            if b.block_type in ("heading", "soft_heading"):
                shown += 1
                print(f"  [{b.block_type}] page={b.page} 「{b.text[:44]}」")
                if shown >= 14:
                    break

    chunks = chunk_blocks(parsed.blocks)
    print(f"chunk 数: {len(chunks)}")
    total_chars = sum(len(c.content) for c in chunks)
    print(f"chunk 总字符: {total_chars}")
    for c in chunks[:4]:
        preview = c.content[:60].replace("\n", " / ")
        print(f"  [sec={c.section} page={c.page}] {preview}")

    # 抽查一段含中文的真实内容，确认非乱码
    if chunks:
        sample = chunks[0].content[:120]
        garble = sample.count("�") + sum(1 for ch in sample if 0xE000 <= ord(ch) <= 0xF8FF)
        print(f"首个 chunk 乱码字符数: {garble}")
        print("内容样本:", sample[:100])


if __name__ == "__main__":
    main()
