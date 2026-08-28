"""P1 工作包B：回答质量评测。

对 gold 问题调真实 LLM 生成回答，用 verify.py 校验：
- 引用准确率：回答中 [n] 编号被对应资料支撑的比例（verify_citations）
- 完备率：枚举类回答完整覆盖资料条目的比例（verify_completeness）
- 事实正确率：回答核心事实与 answer_hint 一致的比例（关键词判定，无 LLM 判定）

用法（backend/ 目录）：
    python -m evaluation.answer_eval                  # 跑全部 gold（有 answer_hint 的）
    python -m evaluation.answer_eval --sample 10      # 只跑前 10 问
    python -m evaluation.answer_eval --kb 水力学第5版  # 只跑某库

评测用生产 LLM（中转站 deepseek-v4-flash）生成回答，真实 rerank 检索。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services import rag, verify  # noqa: E402
from app.services.chat import build_chat_model, build_prompt  # noqa: E402
from evaluation.gold_data import GOLD  # noqa: E402

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


async def _resolve_kb(db, name: str) -> int | None:
    kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    return kb.id if kb else None


def _fact_check(answer: str, hint: str | None) -> bool | None:
    """事实正确性判定：answer_hint 的核心概念是否都在回答里。

    宽松判定（无 LLM）：
    - hint 按「，。、/；：」切段，每段取「核心实词」（去连接词后最长的 ≥2 字词）
    - 判定：**所有核心实词中 ≥80% 出现在回答里** → 算对（容错表述差异：
      hint「糙率不变」vs 回答「糙率必须沿程不变」）
    hint 为 None → 返回 None（不判定）。
    """
    if not hint:
        return None
    import re

    parts = [p.strip() for p in re.split(r"[，。、/；：]", hint) if p.strip()]
    core_words = []
    for p in parts:
        words = [w for w in re.split(r"[的在于和与是了为须]", p) if len(w) >= 2]
        if words:
            core_words.append(max(words, key=len))
    # 过滤明显的冗余核心词（hint 开头常是「X是什么」的 X）和位置标注
    # （「见 6.1」「第X章」是标注依据，不是答案内容）
    import re as _re

    _DROP_PATTERNS = (
        _re.compile(r"^见\s*[\d.]"),        # 见 6.1
        _re.compile(r"^第[\d一二三四五六七八九十]+[章节篇]"),  # 第 6 章
        _re.compile(r"^[一二三四五六七八九十]+\.\d+"),  # 6.1
        _re.compile(r"^[（(]试行[）)]$"),
    )
    core_words = [
        w for w in core_words
        if w not in ("什么", "如何", "哪些", "哪些内容", "哪些要求")
        and not any(p.match(w) for p in _DROP_PATTERNS)
    ]
    if not core_words:
        return None

    # 匹配容错：hint「长直棱柱体渠道」vs 回答「长而直的棱柱体渠道」——
    # 核心词与回答有 ≥2 字公共子串即算命中（滑窗取核心词的所有 ≥2 字子串）。
    def _word_hit(word: str) -> bool:
        if word in answer:
            return True
        # 取核心词的 2/3/4 字滑窗，任一出现在回答里即命中
        # （「长直棱柱体」→ 窗口「棱柱体」「长直」等，「长而直的棱柱体」含「棱柱体」）
        for wlen in (4, 3, 2):
            for i in range(0, len(word) - wlen + 1):
                if word[i : i + wlen] in answer:
                    return True
        return False

    hit = sum(1 for w in core_words if _word_hit(w))
    # ≥80% 核心概念命中 → 算对（一般核心词 3-5 个，容忍 1 个表述差异）
    return hit / len(core_words) >= 0.8


async def eval_one(db, gold: dict) -> dict:
    """单问回答评测：检索 → 生成 → 校验。"""
    kb_id = await _resolve_kb(db, gold["kb"])
    if kb_id is None:
        return {"q": gold["q"], "skipped": True, "reason": "库不存在"}
    try:
        result = await rag.retrieve(db, gold["q"], kb_id=kb_id, top_k=5, include_snippet=True)
        cites = result.cites if isinstance(result, rag.RetrievedResult) else result
    except Exception as exc:
        return {"q": gold["q"], "skipped": True, "reason": f"检索失败: {str(exc)[:80]}"}

    # 组装 prompt 并生成回答（生产 LLM）。
    # 枚举题用 note_incomplete=True：要求完整列出（模拟真实用户"有哪些"= 要全的），
    # 评测反映"完整回答"能力。
    note_incomplete = gold.get("intent") == "enumeration"
    messages = build_prompt(gold["q"], cites, style="standard", note_incomplete=note_incomplete)
    llm = build_chat_model(0.2)
    try:
        resp = await llm.ainvoke(messages)
        answer = getattr(resp, "content", "") or ""
    except Exception as exc:
        return {"q": gold["q"], "skipped": True, "reason": f"生成失败: {str(exc)[:80]}", "cites": len(cites)}

    # 校验：引用忠实 + 完备性 + 事实
    citation = await verify.verify_citations(answer, cites)
    completeness = await verify.verify_completeness(gold["q"], answer, cites)
    fact = _fact_check(answer, gold.get("answer_hint"))

    # 枚举题不完备 → 模拟生产 answer_verify：扩大证据（整文档）重生成一次
    regenerated = False
    if completeness.enumeration and not completeness.complete:
        try:
            broader = await rag.retrieve_document_wide(
                db, gold["q"], kb_id=kb_id, top_k=5
            )
            if broader:
                messages2 = build_prompt(
                    gold["q"], broader, style="standard", note_incomplete=True
                )
                resp2 = await llm.ainvoke(messages2)
                answer2 = getattr(resp2, "content", "") or ""
                if answer2:
                    answer = answer2
                    cites = broader
                    regenerated = True
                    # 重校验
                    citation = await verify.verify_citations(answer, cites)
                    completeness = await verify.verify_completeness(gold["q"], answer, cites)
                    fact = _fact_check(answer, gold.get("answer_hint"))
        except Exception:
            logger.warning("扩大证据重生成失败，用原回答", exc_info=True)

    return {
        "regenerated": regenerated,
        "q": gold["q"],
        "kb": gold["kb"],
        "intent": gold["intent"],
        "skipped": False,
        "cites_count": len(cites),
        "answer_len": len(answer),
        "answer_head": answer[:200],
        "citation_ok": citation.ok,
        "bad_numbers": citation.bad_numbers,
        "completeness_enum": completeness.enumeration,
        "completeness_ok": completeness.complete,
        "completeness_note": completeness.note[:120],
        "fact_ok": fact,
        "answer_hint": gold.get("answer_hint"),
    }


async def main(sample: int, kb_filter: str | None) -> int:
    logging.basicConfig(level=logging.WARNING)
    golds = GOLD
    if kb_filter:
        golds = [g for g in golds if g["kb"] == kb_filter]
    if sample:
        golds = golds[:sample]

    async with async_session_factory() as db:
        # 评测前重建 BM25（可复现）。P1-2 单元2：只索引 active 版本，
        # retired 旧切片不灌入，保证评测指标与生产真实配置一致。
        from app.services import bm25
        from app.db.models import Chunk, Document

        rows = (
            await db.execute(
                select(Chunk.kb_id, Chunk.id, Chunk.content)
                .join(Document, Chunk.doc_id == Document.id)
                .where(Chunk.document_version_id == Document.active_version_id)
            )
        ).all()
        grouped: dict[int, list[tuple[int, str]]] = {}
        for kid, cid, content in rows:
            grouped.setdefault(kid, []).append((cid, content))
        for kid, items in grouped.items():
            await asyncio.to_thread(bm25.rebuild, kid, items)

        results = []
        for gold in golds:
            r = await eval_one(db, gold)
            results.append(r)
            if r["skipped"]:
                print(f"⏭  {r['q']}  ({r['reason']})")
                continue
            mark = "✅" if r["citation_ok"] else "❌"
            enum_s = "枚举" if r["completeness_enum"] else "    "
            fact_s = "✓" if r["fact_ok"] else ("✗" if r["fact_ok"] is False else "?")
            print(f"{mark}[{r['intent']:12}] {r['q']}  cites={r['cites_count']} 引={r['citation_ok']} {enum_s}完={r['completeness_ok']} 事实={fact_s}")

    active = [r for r in results if not r["skipped"]]
    total = len(active)
    if total == 0:
        print("无有效评测（全 skipped）")
        return 0

    cit_ok = sum(1 for r in active if r["citation_ok"]) / total
    # 完备率：只统计枚举类问题
    enum_qs = [r for r in active if r["completeness_enum"]]
    comp_ok = sum(1 for r in enum_qs if r["completeness_ok"]) / len(enum_qs) if enum_qs else None
    # 事实率：只统计有 hint 的
    fact_qs = [r for r in active if r["fact_ok"] is not None]
    fact_ok = sum(1 for r in fact_qs if r["fact_ok"]) / len(fact_qs) if fact_qs else None

    print("\n===== 回答质量汇总 =====")
    print(f"有效问数: {total}")
    print(f"引用准确率: {cit_ok:.1%}")
    if comp_ok is not None:
        print(f"完备率(枚举{len(enum_qs)}问): {comp_ok:.1%}")
    if fact_ok is not None:
        print(f"事实正确率({len(fact_qs)}问): {fact_ok:.1%}")

    report = {
        "total": total,
        "citation_accuracy": round(cit_ok, 4),
        "completeness_rate": round(comp_ok, 4) if comp_ok is not None else None,
        "fact_accuracy": round(fact_ok, 4) if fact_ok is not None else None,
        "results": active,
    }
    (BASE_DIR / "answer_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n报告已存: {BASE_DIR / 'answer_report.json'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P1-工作包B 回答质量评测")
    parser.add_argument("--sample", type=int, default=0, help="只跑前 N 问")
    parser.add_argument("--kb", type=str, default=None, help="只跑某库（库名）")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.sample, args.kb)))
    except KeyboardInterrupt:
        raise SystemExit(130)
