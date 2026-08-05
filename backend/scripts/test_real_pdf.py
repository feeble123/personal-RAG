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
        print("用法: python scripts/test_real_pdf.py <pdf路径>")
        return
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"文件不存在: {p}")
        return
    print(f"文件: {p.name} | 大小: {p.stat().st_size/1024:.1f} KB")

    from app.services.chunker import chunk_blocks
    from app.services.parser.factory import get_parser

    t0 = time.time()
    parsed = get_parser(p.name).parse(p, p.name)
    print(f"解析耗时: {time.time()-t0:.2f}s | 页数: {parsed.page_count} | 块数: {len(parsed.blocks)}")
    print("质量指标:", parsed.quality)

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
