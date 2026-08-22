"""验证脚本：镜像 manager 完整入库流程（解析→注入→切片+目录切片→完整性自检）。

只增不减自检：目录内容单独成「目录」切片、条文说明/附录保留章节身份、
content_completeness 报出所有缺失行。上传前可离线验证策略效果。

用法：python scripts/verify_completeness.py <pdf路径> [new|old]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main() -> None:
    p = Path(sys.argv[1])
    strategy = sys.argv[2] if len(sys.argv) > 2 else "new"
    print(f"== {p.name} | 策略={strategy}")

    from app.services.chunker import chunk_blocks, chunk_toc_pages
    from app.services.parser import boilerplate as bp
    from app.services.parser import gap_check, outline as outline_mod
    from app.services.parser.completeness import check_content_completeness
    from app.services.parser.factory import get_parser

    parsed = get_parser(p.name).parse(p, p.name, strategy)
    print(f"页={parsed.page_count} 块={len(parsed.blocks)} "
          f"quality[toc_pages]={parsed.quality.get('toc_pages')} "
          f"toc_texts 页数={len(parsed.toc_texts)}")

    # 水印/广告过滤（镜像 manager：只增不减的例外——噪声不进知识库）
    parsed.blocks, repeated, removed = bp.filter_repeated_lines(parsed.blocks, parsed.page_count)
    if parsed.toc_texts:
        parsed.toc_texts = {p: bp.filter_text_lines(t, repeated) for p, t in parsed.toc_texts.items()}
    if removed:
        print(f"水印/广告行移除: {len(set(removed))} 条，样例 {list(dict.fromkeys(removed))[:4]}")

    toc = parsed.outline
    blocks = parsed.blocks
    if toc:
        print(f"目录 entries={len(toc.entries)} offset={toc.offset} toc_pages={toc.toc_pages}")
        for e in toc.entries:
            print(f"  [{e.number!r:6}] {e.title[:26]:<28} 印页={e.printed_page} 物理页={e.physical_page}")
        found = gap_check.scan_numbered_lines(blocks)
        confirmed = set(gap_check.candidate_missing(toc, found))
        blocks = outline_mod.inject_blocks(blocks, toc, confirmed, found)
        print(f"扫描编号 {len(found)}，候选缺失 {len(confirmed)}，注入后块 {len(blocks)}")

    chunks = chunk_blocks(blocks)
    toc_chunks = chunk_toc_pages(parsed.toc_texts)
    all_chunks = toc_chunks + chunks
    print(f"\n目录切片={len(toc_chunks)} 正文切片={len(chunks)} 合计={len(all_chunks)}")
    ad_hits = sum(
        1 for c in all_chunks
        if any(k in c.content for k in ("钢管购买热线", "https://emlog", "引用于《", "本资料限内部使用"))
    )
    print(f"含广告水印的 chunk: {ad_hits}/{len(all_chunks)}")
    for c in toc_chunks:
        print(f"  [目录切片 page={c.page} chars={len(c.content)}] {c.content[:48].replace(chr(10),' / ')}")

    secs = Counter((c.section or "")[:40] for c in all_chunks)
    print("\nsection 分布:")
    for s, n in sorted(secs.items()):
        print(f"  {s!r:44} x{n}")

    # TOC 覆盖 + section 纯净性检查（目录策略可靠性的证明）：
    # ① 每条目录条目必须作为某切片的「段」出现（编号段 / 附录标签 / 标题互含）；
    # ② 任何切片的每个段都必须来自目录 1/2 级大纲——正文句子（如「2 正常使用极限状态:…」）
    #    绝不允许当 section（只按编号误确认的旧 bug，曾被正文列表项污染章节栈）。
    if toc is not None:
        import re as _re

        sections = [c.section or "" for c in all_chunks]
        # 段列表：section「3 管道结构上的作用 / 3.3 可变…」→ ["3 管道结构上的作用", "3.3 可变…"]
        segs = [seg.strip() for s in sections for seg in s.split("/") if seg.strip()]

        def _appendix_label(title):
            m = _re.match(r"^附录\s*([A-Z])", title or "")
            return f"附录{m.group(1)}" if m else None

        known_nums = {e.number for e in toc.entries if e.number}
        known_labs = {lab for e in toc.entries if (lab := _appendix_label(e.title))}
        known_titles = {e.title for e in toc.entries if e.title and len(e.title) >= 2}

        # ① 覆盖：编号条目查「以编号开头的段」，无编号条目查附录标签 / 标题互含
        missing_toc = []
        for e in toc.entries:
            if not e.title or len(e.title) < 2:
                continue
            if e.number:
                ok = any(seg.startswith(e.number + " ") for seg in segs)
            else:
                lab = _appendix_label(e.title)
                if lab:
                    ok = any(seg.startswith(lab) for seg in segs)
                else:
                    ok = any(e.title in seg for seg in segs)
            if not ok:
                missing_toc.append(f"{e.number or ''} {e.title[:24]}")
        print(f"TOC 覆盖: 条目 {len(toc.entries)}，缺 section 的 {len(missing_toc)} 条"
              + (f"：{missing_toc}" if missing_toc else "（全覆盖）"))

        # ② 纯净性：每个 section 段必须是目录 1/2 级条目（编号 / 附录标签 / 标题互含）
        bad_segs = []
        for seg in segs:
            if seg == "目录":
                continue
            m = _re.match(r"^(\d{1,3}(?:\.\d{1,3}){0,3})", seg)
            ok = bool(m and m.group(1) in known_nums)
            if not ok:
                ok = any(seg.startswith(lab) for lab in known_labs)
            if not ok:
                ok = any(t in seg for t in known_titles)
            if not ok:
                bad_segs.append(seg[:30])
        print(f"section 纯净性: 正文句子当段 = {len(bad_segs)} 个"
              + (f"：{bad_segs}" if bad_segs else "（全部来自目录 1/2 级大纲）"))

    # 顺序验证：正文 chunk 页序单调 = 源文件顺序未被注入打乱（BUG5）
    body_pages = [c.page for c in chunks if c.page]
    monotonic = all(a <= b for a, b in zip(body_pages, body_pages[1:]))
    print(f"顺序验证: 正文 chunk 页序单调={monotonic}（{len(body_pages)} 个有页码 chunk）")

    comp = check_content_completeness(parsed.blocks, all_chunks)
    print(f"\n完整性: complete={comp['complete']} missing_lines={comp['missing_lines']} "
          f"pages={comp['missing_pages']} skipped_long={comp['skipped_long_lines']}")
    pc = comp["page_coverage"]
    print(f"页级覆盖: complete={pc['complete']} content_pages={pc['content_pages']} "
          f"chunk_pages={pc['chunk_pages']} uncovered={pc['uncovered_pages']}")
    for m in comp["sample"]:
        print("  MISSING:", m)


if __name__ == "__main__":
    main()
