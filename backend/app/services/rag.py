"""RAG 检索：向量检索为主 + BM25 补充候选 + bge-reranker 重排。

v3 要点（2026-08-04 实测）：
- 候选池 = 向量 top50 ∪ 本库 BM25 top50，按 0.7×向量 + 0.3×BM25 归一化混合
- rerank 候选 100（BGE-M3 对抽象/长查询会漏召回，候选池小则 reranker 见不到正确答案）
- rerank 用「聚焦主题词」替代原查询：长查询会稀释关键项（如
  「水利技术标准编写规定中规定的引用标准是什么？」里文档标题喧宾夺主，
  reranker 反而偏好前言/总则）；聚焦「引用标准」后正确规则节得分 0.98+。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Chunk, Document
from app.schemas import CitationOut
from app.services import bm25, vector_store
from app.services.embedding import embed_query

logger = logging.getLogger(__name__)


async def _active_version_ids(
    db: AsyncSession, kb_id: int | None = None, doc_ids: list[int] | None = None
) -> set[int]:
    """当前 active 的文档版本 id 集合（检索 DB 直接查询时过滤 retired 用）。

    P0-8：DB 同时存 active + retired 版本的 chunks；向量/BM25 索引只含 active，
    但**直接查 DB** 的路径（doc_chunk_ids 解析、章节/枚举扩展、整文档补全）必须
    显式过滤 retired，否则会把不可查的旧版切片混进证据。
    """
    stmt = select(Document.active_version_id).where(Document.active_version_id.is_not(None))
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    if doc_ids:
        stmt = stmt.where(Document.id.in_(doc_ids))
    rows = (await db.execute(stmt)).all()
    return {r[0] for r in rows}


# 中文问句的提问词（用于提取 rerank 核心主题词）
_QUESTION_PATTERN = re.compile(
    r"(.{2,30}?)(?:是什么|有哪些|有什么要求|有什么规定|有何要求|包括哪些|包含哪些|"
    r"指什么|是什么样的|如何)"
)
_QUESTION_PRE = re.compile(r"^(请问|你好|帮我|我想知道|麻烦问下|想了解下|麻烦查下)+")
# 泛词：切分后跳过（如「关于公式的要求」不能取末尾的「要求」）
_GENERIC_WORDS = {"要求", "规定", "内容", "情况", "方法", "原则", "事项", "问题", "标准", "条款", "方面", "部分"}
# 泛词前的修饰前缀：「具体要求」「主要规定」仍是泛词，须跳过（否则「组织指挥体系的具体要求」
# 会取「具体要求」当主题词，rerank 全排成噪声；「引用标准」的「引用」不是修饰词，不受影响）
_GENERIC_MODIFIERS = ("具体", "主要", "基本", "相关", "一般", "重要", "相应", "有关", "详细")


def _is_generic_part(part: str) -> bool:
    """段是否为泛词：精确匹配，或「修饰词+泛词」（具体/主要/基本…要求/规定…）。"""
    if part in _GENERIC_WORDS:
        return True
    base = re.sub(rf"^(?:{'|'.join(_GENERIC_MODIFIERS)})", "", part)
    return base in _GENERIC_WORDS


def focus_rerank_query(query: str) -> str:
    """提取 rerank 用核心主题词；提取失败/不聚焦时回退原查询。

    BGE 系列模型对长查询会稀释关键项（实测：完整长问题下 reranker 把「前言列出的
    引用标准清单」排到「3.2 引用标准规则节」之前；聚焦「引用标准」后规则节 0.98+）。
    仅用于 rerank 打分；向量/BM25 召回仍用原查询（保证候选池不变）。
    """
    s = query.strip().strip("？?。！!，,；; \t")
    s = _QUESTION_PRE.sub("", s)
    m = _QUESTION_PATTERN.search(s)
    if not m:
        return query
    raw = m.group(1)
    # 按「的/中/关于/对于/、/和」切段，从后往前找第一个非泛词段。
    # 例「…中规定的关于公式的要求」→ 切出 […, 规定, 关于公式, 要求] → 取「关于公式」→ 去「关于」→「公式」。
    # 不能直接取最后一段：会拿到「要求」这类泛词（实测 rerank 用「要求」全排成噪声）。
    parts = [p.strip() for p in re.split(r"(?:的|中|关于|对于|、|及|和|与|，)", raw) if p.strip()]
    subj = None
    for p in reversed(parts):
        p2 = re.sub(r"^(?:关于|对于|对)", "", p).strip()
        if p2 and not _is_generic_part(p2):
            subj = p2
            break
    if not subj or len(subj) < 2:
        return query
    # 提取的仍很长（含文档标题等）→ 保持原查询
    if len(subj) * 2 > len(query) and len(query) > 10:
        return query
    return subj


# ---- 问题中「点名文档」→ 检索限定（BUG-A）----
# 《书名号》引用；「XXX中」引用（XXX 在「中」前，如「重庆市防汛抗旱应急预案中后期处置…」）
_DOC_TITLE_RE = re.compile(r"《([^《》]{2,80})》")
# 文档泛指词：中分句里若是这类泛词（规范/标准/预案…）不当作点名文档
_DOC_GENERIC = {"规范", "标准", "预案", "文件", "文档", "规定", "指南", "导则", "资料", "制度", "办法", "规程", "要求", "内容"}
_DOC_EXT = (".pdf", ".docx", ".md", ".markdown", ".txt", ".xlsx", ".csv")


def _strip_ext(name: str) -> str:
    for ext in _DOC_EXT:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def _doc_name_candidates(query: str) -> list[str]:
    """提取问题中点名的文档名候选：《…》内容 + 「XXX中」的 XXX（去在/关于前缀）。"""
    cands: list[str] = []
    for t in _DOC_TITLE_RE.findall(query):
        cands.append(t.strip())
    for seg in query.split("中"):
        s = re.sub(r"^(在|关于|对于|就)\s*", "", seg.strip())
        s = s.replace("《", "").replace("》", "")
        if 2 <= len(s) <= 40 and s not in _DOC_GENERIC:
            cands.append(s)
    return cands


def _match_document(docs, cand: str):
    """候选名匹配文档：精确优先；子串匹配要求「长短比 ≥ 0.4」防泛词误中（如「预案」命中整份预案名）。"""
    exact = [d for d in docs if _strip_ext(d.filename) == cand]
    if exact:
        return exact[0]
    best, best_ratio = None, 0.0
    for d in docs:
        fn = _strip_ext(d.filename)
        if cand in fn or fn in cand:
            short, long = min(len(cand), len(fn)), max(len(cand), len(fn))
            ratio = short / long if long else 0.0
            if ratio >= 0.4 and ratio > best_ratio:
                best, best_ratio = d, ratio
    return best


async def resolve_documents_by_title(
    db: AsyncSession, query: str, kb_id: int | None = None
) -> list[int]:
    """从问题中提取书名/文档名（《…》或「XXX中」），匹配知识库文档，返回 doc_id 列表。

    kb_id 非空时只匹配该库文档（P0-2 scope 隔离：KB-A 里点名 KB-B 的同名文档 → 返回空，
    检索回退当前库，杜绝跨库污染）。kb_id=None（跨全部库模式）匹配全库。
    匹配不到（宽泛问法如「在数字孪生工程中…」、泛词如「标准中规定…」）返回空列表，
    检索回退跨库。支持多书名（对比两份规范 → 返回多个 doc_id）。
    """
    from app.db.models import Document

    stmt = select(Document)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    docs = (await db.scalars(stmt)).all()
    if not docs:
        return []
    result: list[int] = []
    for cand in _doc_name_candidates(query):
        best = _match_document(docs, cand)
        if best and best.id not in result:
            result.append(best.id)
    return result


def _section_no(comp: str) -> str:
    """去掉章节组件里的编号前缀：「5 应急保障」→「应急保障」，「3.2 引用标准」→「引用标准」。"""
    return re.sub(r"^[\d.]+\.?\s*", "", comp)


def _match_section_component(focused: str, section: str | None) -> str | None:
    """在 top1 的章节路径里找与聚焦主题词匹配的组件（越深越具体）。

    例：聚焦「应急保障」+ 路径「5 应急保障 / 5.1 制度保障」→「5 应急保障」（整章范围）；
        聚焦「引用标准」+ 路径「3 正文部分 / 3.2 引用标准」→「3.2 引用标准」（窄组件，不扩全章）。
    """
    if not focused or not section:
        return None
    best: str | None = None
    best_depth = -1
    for i, comp in enumerate(p.strip() for p in section.split("/")):
        if not comp:
            continue
        if focused in _section_no(comp) or _section_no(comp) in focused:
            if i > best_depth:
                best, best_depth = comp, i
    return best


def _under_component(sec: str | None, comp: str) -> bool:
    """章节路径是否「直接归属」指定组件（组件级匹配，防「5 应急保障」误中「15 应急保障」；
    且组件之后不再嵌套更深「附件N」——避免把嵌套的附件5 等子列表混入当前列表，造成来源混乱）。"""
    if not sec:
        return False
    parts = [p.strip() for p in sec.split("/") if p.strip()]
    idx = -1
    for j, p in enumerate(parts):
        if p == comp or p.startswith(comp):
            idx = j
            break
    if idx < 0:
        return False
    for p in parts[idx + 1:]:
        if re.match(r"附件\d", p):
            return False
    return True


# 枚举/概述类问题检测（跨库通用问句，不依赖知识库内容：台账/名单/章节等任何结构都适用）
_ENUMERATION_RE = re.compile(
    r"有哪些|包含哪些|包括哪些|都有哪些|哪几个|哪几类|哪几级|哪几种|多少|所有|全部|完整|完整的|列出来|列出|"
    r"概述|介绍|一览|全貌|总结|汇总|清单|一览表|都要|都有|还有|其他|其余|别的"
)


async def _expand_enumeration_sections(
    db: AsyncSession,
    query: str,
    cand_sorted: list[tuple[int, float]],
    section_by_id: dict[int, str | None],
    top_k: int,
    kb_id: int | None = None,
    candidates_full: dict[int, float] | None = None,
) -> list[tuple[int, float]] | None:
    """枚举/概述类问题的证据完备检索：纳入查询最可能指向的章节单元的**全部切片**。

    取代按内容关键词（台账/名单）猜列表的正则补丁，纯结构判定，对任何知识库通用：
    - 看 top-K 候选切片归属哪些「章节单元」（完整章节路径），取出现最多者
      ——「这个问题最关心哪个章节」由检索结果投票，不依赖章节叫什么名；
    - 平局时选库里切片数多的章节（更可能是清单/台账类大单元）。
    再从 DB 拉该章节全部切片（直接归属、排除嵌套附件），保证枚举类回答不遗漏任何成员。
    扩展上限 complete_expansion_cap（默认 40）。
    """
    if not cand_sorted:
        return None
    if not _ENUMERATION_RE.search(query):
        return None
    # P0-2 scope 隔离：扩展只在本库内拉取（kb_id=None 跨全部库时不过滤）
    kb_cond = Chunk.kb_id == kb_id if kb_id is not None else None
    # P0-8 active 过滤：DB 同时存 active+retired 版本，扩展只拉 active 版本切片
    active_ids = await _active_version_ids(db, kb_id=kb_id)
    # 取候选集：优先完整 fused 候选集（覆盖更全），否则 top-K
    pool = candidates_full or dict(cand_sorted)
    ranked_pool = sorted(pool.items(), key=lambda x: x[1], reverse=True)[:200]
    if not ranked_pool:
        return None
    # 补齐章节信息（候选可能超出 top-100 的 section_by_id 范围）
    sec_map: dict[int, str | None] = dict(section_by_id)
    missing = [cid for cid, _ in ranked_pool if cid not in sec_map]
    if missing:
        stmt = select(Chunk.id, Chunk.section).where(Chunk.id.in_(missing))
        if kb_cond is not None:
            stmt = stmt.where(kb_cond)
        if active_ids:
            stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
        rows = (await db.execute(stmt)).all()
        sec_map.update({cid: sec for cid, sec in rows})
    # 全量章节-切片结构表（统计章节大小 + 拉取全量用）；只含 active 版本
    all_stmt = select(Chunk, Document).join(Document, Chunk.doc_id == Document.id)
    if kb_cond is not None:
        all_stmt = all_stmt.where(kb_cond)
    if active_ids:
        all_stmt = all_stmt.where(Chunk.document_version_id.in_(active_ids))
    all_chunks = (await db.execute(all_stmt)).all()
    # 章节单元：按「章前缀」（section 首段）聚合——OCR 噪声子节分散不影响判定。
    # 例：`4 流动阻力与水头损失 / 将J=气代入上式` → `4 流动阻力与水头损失`。
    def _chapter_of(sec: str) -> str:
        parts = [p.strip() for p in sec.split("/") if p.strip()]
        return parts[0] if parts else sec

    size_by_sec: dict[str, int] = {}
    chapter_sizes: dict[str, int] = {}
    for c, _ in all_chunks:
        if not c.section:
            continue
        ch = _chapter_of(c.section)
        chapter_sizes[ch] = chapter_sizes.get(ch, 0) + 1
    # 候选归属的章节，取其章前缀的大小（容忍子节分散）
    for sec in {s for s in sec_map.values() if s}:
        size_by_sec[sec] = chapter_sizes.get(_chapter_of(sec), 0)
    # P1-9 章节相关性：RRF 融合后分数尺度接近（1/(60+rank)），分数之和选章节会失真。
    # 改为「候选归属章节出现次数 + 该章节块数（容忍噪声聚合）」投票。
    # - 投票用「前 2 段」聚合（容 OCR 噪声子节分散）
    # - 但候选多出现在精确子节时，聚合到章级会丢「附录1 专家名单」这类 2 段独立单元
    #   权衡：投票键取「前 2 段」，若该单元块太少（噪声碎片），回退到首段章
    vote_by_sec: dict[str, int] = {}
    for cid, _sc in ranked_pool:
        sec = sec_map.get(cid)
        if not sec:
            continue
        unit = _chapter_of(sec)  # 前 2 段
        # 该单元块太少（噪声碎片）→ 回退首段（章）
        if chapter_sizes.get(unit, 0) < 4 and "/" in unit:
            unit = unit.split("/")[0].strip()
        vote_by_sec[unit] = vote_by_sec.get(unit, 0) + 1
    if not vote_by_sec:
        return None
    # 选「出现次数最高」的单元；平局取块数多者
    best_unit = max(vote_by_sec, key=lambda s: (vote_by_sec[s], chapter_sizes.get(s, 0)))
    if chapter_sizes.get(best_unit, 0) < 4:
        logger.debug("枚举扩展：单元 %s 块数 %s < 4，不扩展", best_unit, chapter_sizes.get(best_unit, 0))
        return None  # 单元块太少（≤3）不扩展，top_k 已覆盖
    # 拉取该单元下全部切片（含子节）。枚举扩展要全量列表（含「附件N」子节），
    # 不用 `_under_component` 排除附件（那是章节扩展的防混入逻辑，枚举需完整）。
    members = [
        (c, d)
        for c, d in all_chunks
        if c.section and _chapter_of(c.section) == best_unit
    ]
    members = members[: settings.complete_expansion_cap * 2]
    ids = [c.id for c, _ in members]
    if settings.rerank_enabled:
        docs = [c.content for c, _ in members]
        try:
            scores = await rerank(focus_rerank_query(query), docs)
            ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
        except Exception:
            logger.warning("枚举扩展 rerank 失败，按原文顺序", exc_info=True)
            ranked = sorted(zip(ids, [0.0] * len(ids)), key=lambda x: x[0])
    else:
        # 离线/降级：按原文顺序（chunk.id），保证列表不乱序
        ranked = sorted(zip(ids, [0.0] * len(ids)), key=lambda x: x[0])
    return ranked[: settings.complete_expansion_cap]


async def _expand_chapter_sections(
    db: AsyncSession,
    query: str,
    cand_sorted: list[tuple[int, float]],
    section_by_id: dict[int, str | None],
    top_k: int,
    kb_id: int | None = None,
) -> list[tuple[int, float]] | None:
    """综合型问题：主题词命中章节的多个子节时，把整章子节纳入最终引用。

    规范类章节横跨多子节多页（如重庆预案「5 应急保障」5.1~5.12），top_k=5 只覆盖
    一小部分 → LLM 必然答不完整。按「与聚焦主题词匹配的章节组件」扩展：
    - 命中整章组件（「5 应急保障」）→ 覆盖该章全部子节；
    - 命中窄组件（「3.2 引用标准」）→ 只扩该组件，不扩大章节；
    - 命中子节数不足 top_k → 维持原 top_k。
    直接从 DB 取该组件下全部切片再 rerank 排序（不受 rerank 候选池限制，
    跨库检索时也不会因候选被挤占而丢子节）。P0-2 scope 隔离：只在本库内扩展。
    返回最多 15 条。
    """
    if not cand_sorted:
        return None
    if not settings.rerank_enabled:
        return None  # 章节扩展依赖 rerank 排序；离线/未开启时跳过，避免无意义网络调用
    top1 = cand_sorted[0][0]
    comp = _match_section_component(focus_rerank_query(query), section_by_id.get(top1))
    if not comp:
        return None
    stmt = select(Chunk, Document).join(Document, Chunk.doc_id == Document.id)
    if kb_id is not None:
        stmt = stmt.where(Chunk.kb_id == kb_id)
    # P0-8 active 过滤：扩展只拉 active 版本切片
    active_ids = await _active_version_ids(db, kb_id=kb_id)
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    rows = (await db.execute(stmt)).all()
    members = [(c, d) for c, d in rows if _under_component(c.section, comp)]
    if len(members) < 4:
        return None  # 章节块太少（≤3）不扩展；≥4 则全部纳入，防 top_k 截断漏块（如名单章节 5 块只取 5 块=top_k 也会被旧守卫拦掉）
    ids = [c.id for c, _ in members]
    docs = [c.content for c, _ in members]
    try:
        scores = await rerank(focus_rerank_query(query), docs)
    except Exception:
        logger.warning("章节扩展 rerank 失败，维持原 top_k", exc_info=True)
        return None
    return sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)[:15]


def judge_evidence_level(scores: list[float]) -> str:
    """依据检索重排后的相关分数判定证据等级（四级：sufficient/partial/weak/none）。

    - sufficient 充足：top1 高分 或 ≥2 块强相关（交叉印证）
    - partial 部分：top1 中等相关（有据可答但可能不完整）
    - weak 较弱：top1 弱相关（LLM 据实作答并提示资料有限）
    - none 不足：无高分块（系统直接拒答，不调 LLM —— 防幻觉 + 省 token）
    阈值见 settings.evidence_*，可在 .env 调优。
    """
    if not scores:
        return "none"
    top1 = max(0.0, float(scores[0]))
    strong = sum(1 for s in scores if float(s) >= settings.evidence_strong_threshold)
    if top1 >= settings.evidence_sufficient_threshold:
        return "sufficient"
    if strong >= 2:
        return "sufficient"
    if top1 >= settings.evidence_partial_threshold:
        return "partial"
    if top1 >= settings.evidence_weak_threshold:
        return "weak"
    return "none"


@dataclass
class RetrievedChunk:
    chunk_id: int
    kb_id: int
    doc_id: int
    source: str
    page: int | None = None
    section: str | None = None
    snippet: str = ""
    score: float = 0.0
    rank: int = 0
    # P0-11 检索出处元数据（未来 DSH 引用来源）：块类型 / 条款号 / 公式编号 / 文档类型
    block_type: str = "text"
    clause_no: str | None = None
    formula_no: str | None = None
    doc_type: str = "other"

    def to_citation(self) -> CitationOut:
        return CitationOut(
            chunk_id=self.chunk_id,
            kb_id=self.kb_id,
            doc_id=self.doc_id,
            source=self.source,
            page=self.page,
            section=self.section,
            snippet=self.snippet,
            score=self.score,
            rank=self.rank,
        )


@dataclass
class CandidateTrace:
    """P1-1 评测门禁：单个候选的阶段性分数（用于离线评测/可回放 trace）。

    记录从向量/BM25 到融合到 rerank 的完整路径，评测时按需求取对应分。
    """

    chunk_id: int
    vector_score: float | None = None
    bm25_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None


@dataclass
class RetrievalTrace:
    """P1-1 评测门禁：一次 retrieve 的完整可回放 trace。"""

    query: str
    candidates: list[CandidateTrace]
    rerank_ok: bool = False
    rerank_status: str = "disabled"  # ok / failed / disabled
    expanded_type: str | None = None  # None / chapter / enumeration / coverage
    vector_hits: int = 0
    bm25_hits: int = 0


@dataclass
class RetrievedResult:
    """P1-1 评测门禁：retrieve 返回值（return_trace=True 时）。cites 兼容旧调用。"""

    cites: list[RetrievedChunk]
    trace: RetrievalTrace | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    n = len(a)
    dot = sum(a[i] * b[i] for i in range(n))
    na = (sum(x * x for x in a)) ** 0.5
    nb = (sum(x * x for x in b)) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def rerank(query: str, documents: list[str]) -> list[float]:
    """硅基流动 bge-reranker API 重排，返回与 documents 对齐的相关性分数（0~1）。"""
    import httpx

    payload = {
        "model": settings.rerank_model,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
        "return_documents": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.embedding_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.embedding_base_url.rstrip('/')}/rerank", json=payload, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
    scores = [0.0] * len(documents)
    for item in data.get("results", []):
        idx = item.get("index")
        if idx is not None and 0 <= idx < len(scores):
            scores[idx] = item.get("relevance_score", 0.0)
    return scores


async def retrieve(
    db: AsyncSession,
    query: str,
    kb_id: int | None = None,
    doc_ids: list[int] | None = None,
    top_k: int | None = None,
    include_snippet: bool = True,
    return_trace: bool = False,
) -> list[RetrievedChunk] | RetrievedResult:
    """检索主入口。kb_id=None 跨全库（按库归一化 BM25 加权）；doc_ids 限定只搜点名文档。

    final = 0.7×向量相似度 + 0.3×BM25归一化分
    - 向量为主（真实余弦，区分度好）
    - BM25 精确关键词兜底：解决 BGE-M3 对「计算公式/推求步骤」等查询区分度不足，
      以及长问题下关键词被稀释的问题（如「请问…通信网络体系有什么要求呢？」）
    - 跨库时各库 BM25 按**该库自身 top1 归一化**，无关库（BM25 近 0）不会产生噪声
    - doc_ids 非空：只搜这些文档的切片（问题点名《书名》或「XXX中」时），BM25 按文档后过滤

    P1-1 评测门禁：return_trace=True 时返回 RetrievedResult（cites + trace）。
    默认 False 返回 list[RetrievedChunk]，零影响现有调用。
    """
    top_k = top_k or settings.top_k_final

    # P1-1 评测门禁：收集各阶段分数供 trace
    trace_cands: dict[int, CandidateTrace] = {}

    # 1) 向量检索（真实余弦相似度）；doc_ids 限定 → Chroma metadata doc_id 过滤
    qvec = await embed_query(query)
    doc_chunk_ids: set[int] = set()
    doc_kb_ids: set[int] = set()
    if doc_ids:
        # P0-2 scope 隔离：点名文档也必须在当前库内（kb 非空时），杜绝 KB-B 文档混入
        # P0-8 active 过滤：DB 同时存 active+retired 版本，只取 active 版本切片
        active_ids = await _active_version_ids(db, kb_id=kb_id, doc_ids=doc_ids)
        stmt = select(Chunk.id, Chunk.kb_id).where(Chunk.doc_id.in_(doc_ids))
        if active_ids:
            stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
        if kb_id is not None:
            stmt = stmt.where(Chunk.kb_id == kb_id)
        rows = (await db.execute(stmt)).all()
        doc_chunk_ids = {cid for cid, _ in rows}
        doc_kb_ids = {kid for _, kid in rows}
        where = {"doc_id": doc_ids[0]} if len(doc_ids) == 1 else {"doc_id": {"$in": doc_ids}}
    else:
        where = {"kb_id": kb_id} if kb_id else None
    vec_hits = await asyncio.to_thread(vector_store.query, qvec, where, settings.top_k_vector)
    candidates: dict[int, float] = {h.chunk_id: h.score for h in vec_hits}
    if return_trace:
        for h in vec_hits:
            trace_cands.setdefault(h.chunk_id, CandidateTrace(chunk_id=h.chunk_id)).vector_score = h.score

    # 2) BM25 加权（单库或跨库均生效；跨库按库归一化；doc_ids 限定 → 放大候选后按文档过滤）
    bm25_norm: dict[int, float] = {}
    if doc_ids:
        kb_ids: list[int] = list(doc_kb_ids)
        bm25_k = settings.top_k_bm25 * 5  # 限定文档时候选放大，防后过滤缩水
    else:
        kb_ids = [kb_id] if kb_id is not None else bm25.all_kb_ids()
        bm25_k = settings.top_k_bm25
    for kid in kb_ids:
        if not bm25.has_kb(kid):
            continue
        hits = await asyncio.to_thread(bm25.search, kid, query, bm25_k)
        if doc_ids:
            hits = [(cid, s) for cid, s in hits if cid in doc_chunk_ids]
        if not hits:
            continue
        max_s = max(s for _, s in hits)
        if max_s <= 0:
            # 该库 BM25 全 0（查询匹配不到关键词，如问候语「你好」）→ 跳过，避免除零崩溃
            continue
        for cid, s in hits:
            bm25_norm[cid] = s / max_s  # 该库内归一化到 0~1
            if return_trace:
                trace_cands.setdefault(cid, CandidateTrace(chunk_id=cid)).bm25_score = s / max_s

    # 3) BM25 补召回：向量 top50 未出现的 chunk，实时算真实余弦
    extra_ids = [cid for cid in bm25_norm if cid not in candidates]
    if extra_ids:
        embeddings = await asyncio.to_thread(vector_store.get_embeddings_by_ids, extra_ids)
        for cid, vec in embeddings.items():
            candidates[cid] = _cosine(qvec, vec)
            if return_trace:
                trace_cands.setdefault(cid, CandidateTrace(chunk_id=cid)).vector_score = candidates[cid]

    # 4) P1-9 RRF 融合（替代 0.7v+0.3bm25 线性加权——两分数不同标度，线性会失真）。
    #    向量命中按 score 降序；BM25 按库内归一化分降序。
    from app.services.retrieval.fusion import rrf_fuse

    vec_sorted = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
    bm25_sorted = sorted(bm25_norm.items(), key=lambda x: x[1], reverse=True)
    fused = rrf_fuse(vec_sorted, bm25_sorted)
    # 候选池 = 融合结果（含 BM25 独有补召回）；保留向量分用于后续余弦对照
    candidates = fused
    if return_trace:
        for cid, fs in fused.items():
            trace_cands.setdefault(cid, CandidateTrace(chunk_id=cid)).fusion_score = fs

    # 5) 送入 reranker 重排（cross-encoder，纠正向量对部分查询的区分度不足）
    cand_sorted = sorted(candidates.items(), key=lambda x: x[1], reverse=True)[
        : settings.rerank_candidates
    ]
    rerank_ok = False
    rerank_status = "disabled"
    section_by_id: dict[int, str | None] = {}
    if cand_sorted:
        # 预先取候选的章节信息：rerank 与两种扩展共用；rerank 关闭时扩展仍可用
        ids = [cid for cid, _ in cand_sorted]
        rows = (
            await db.execute(
                select(Chunk, Document)
                .join(Document, Chunk.doc_id == Document.id)
                .where(Chunk.id.in_(ids))
            )
        ).all()
        content_by_id = {c.id: c.content for c, _ in rows}
        section_by_id = {c.id: c.section for c, _ in rows}
        # P1-9：min_content_len 提前到候选池阶段过滤（短块不参与 rerank/扩展），
        # 被过滤的从候选池回补 top_k（防 final hydrate 缩水）。
        cand_sorted = [
            (cid, sc) for cid, sc in cand_sorted
            if len((content_by_id.get(cid) or "").strip()) >= settings.min_content_len
        ]
        if len(cand_sorted) < settings.top_k_final:
            # 回补：从原候选池按融合分补足 top_k_final
            for cid, sc in sorted(candidates.items(), key=lambda x: x[1], reverse=True):
                if len(cand_sorted) >= settings.top_k_final:
                    break
                if cid not in {c for c, _ in cand_sorted} and cid not in content_by_id:
                    # 未加载内容的候选也保留（防全滤空）
                    cand_sorted.append((cid, sc))
        ids = [cid for cid, _ in cand_sorted]
        docs = [content_by_id.get(cid, "") for cid in ids]
        if settings.rerank_enabled:
            try:
                # 用聚焦主题词重排：长查询会稀释关键项（BGE 局限），
                # 聚焦「引用标准」后正确规则节 0.98+（原文案仅 0.81 被前言压过）
                r_scores = await rerank(focus_rerank_query(query), docs)
                cand_sorted = sorted(zip(ids, r_scores), key=lambda x: x[1], reverse=True)
                rerank_ok = True
                rerank_status = "ok"
                if return_trace:
                    for cid, rs in zip(ids, r_scores):
                        if cid in trace_cands:
                            trace_cands[cid].rerank_score = rs
            except Exception:
                logger.warning("rerank 失败，回退到混合排序", exc_info=True)
                rerank_status = "failed"
                cand_sorted = cand_sorted[:top_k]

    # 6) 取 top_k；先章节扩展（综合型问题整章覆盖），再枚举扩展（枚举/清单类问题拉全量章节）
    ranked = cand_sorted[:top_k]
    expanded_type: str | None = None
    expanded = await _expand_chapter_sections(
        db, query, cand_sorted, section_by_id, top_k, kb_id=kb_id
    )
    if expanded:
        expanded_type = "chapter"
    else:
        expanded = await _expand_enumeration_sections(
            db, query, cand_sorted, section_by_id, top_k, kb_id=kb_id,
            candidates_full=candidates,
        )
        if expanded:
            expanded_type = "enumeration"
    if expanded:
        ranked = expanded
    hydrated = await _hydrate(db, ranked, include_snippet)
    # P0-2 scope 不变量：kb 非空时所有引用必须属于该库（纵深防御，防未来扩展路径再漏）
    if kb_id is not None:
        leaked = [c for c in hydrated if c.kb_id != kb_id]
        if leaked:
            logger.error(
                "scope 越界：检索返回其他库切片 kb=%s leaked_chunks=%s",
                kb_id, [c.chunk_id for c in leaked],
            )
            hydrated = [c for c in hydrated if c.kb_id == kb_id]

    # P1-1 评测门禁：return_trace=True 时返回 RetrievedResult（cites + trace）
    if return_trace:
        trace = RetrievalTrace(
            query=query,
            candidates=sorted(trace_cands.values(), key=lambda c: c.chunk_id),
            rerank_ok=rerank_ok,
            rerank_status=rerank_status,
            expanded_type=expanded_type,
            vector_hits=len(vec_hits),
            bm25_hits=len(bm25_norm),
        )
        return RetrievedResult(cites=hydrated, trace=trace)
    return hydrated


async def _hydrate(
    db: AsyncSession, ranked: list[tuple[int, float]], include_snippet: bool
) -> list[RetrievedChunk]:
    if not ranked:
        return []
    ids = [cid for cid, _ in ranked]
    rows = (
        await db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.id.in_(ids))
        )
    ).all()
    by_id: dict[int, tuple[Chunk, Document]] = {}
    for chunk, doc in rows:
        by_id[chunk.id] = (chunk, doc)

    order = {cid: i for i, cid in enumerate(ids)}
    items: list[RetrievedChunk] = []
    for cid, score in ranked:
        pair = by_id.get(cid)
        if not pair:
            continue
        chunk, doc = pair
        # 过滤低信息量短块（封面/目录碎片，向量相似度虚高）。
        # P1-4：有父上下文的子块，用父块长度判断（父块够长即保留，内容短子块不误杀）。
        effective_len = len(chunk.parent_context or chunk.content)
        if effective_len < settings.min_content_len:
            continue
        # P1-4：命中子块（有父上下文）且内容偏短 → 注入父块全文（LLM 看到完整小节）。
        # 引用锚点仍是子块 chunk_id；父块本身是 block_type='parent' 不直接作为引用。
        snippet = chunk.content if include_snippet else chunk.content[:200]
        if (
            chunk.parent_context
            and chunk.parent_chunk_id is not None
            and len(chunk.content.strip()) < settings.min_content_len * 2
        ):
            snippet = chunk.parent_context if include_snippet else chunk.parent_context[:200]
        items.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                kb_id=chunk.kb_id,
                doc_id=chunk.doc_id,
                source=doc.filename,
                page=chunk.page,
                section=chunk.section,
                snippet=snippet,
                score=round(score, 4),
                # P0-11 出处元数据：从 DB 落库字段带出（旧数据 NULL 回退默认）
                block_type=chunk.block_type or "text",
                clause_no=chunk.clause_no,
                formula_no=chunk.formula_no,
                doc_type=getattr(doc, "doc_type", None) or "other",
            )
        )
    items.sort(key=lambda x: order[x.chunk_id])
    for i, it in enumerate(items):
        it.rank = i + 1
    return items


async def retrieve_document_wide(
    db: AsyncSession,
    query: str,
    kb_id: int | None = None,
    top_k: int = 5,
    cap: int = 60,
    _cites: list[RetrievedChunk] | None = None,
) -> list[RetrievedChunk]:
    """补全重生成用：先普通检索定位问题所属文档，再返回该文档**全部切片**（上限 cap）。

    层2 完备性校验判定「枚举题遗漏」时，单章节扩展可能仍不全（答案横跨多个 sheet/章节），
    此时扩大到整份相关文档，保证模型看到完整数据。按 chunk.id 保持原文顺序。
    _cites：调用方已做普通检索（如 /optimize 需要同时拿证据等级分数）时传入，避免二次检索。
    """
    cites = _cites if _cites is not None else await retrieve(db, query, kb_id=kb_id, top_k=top_k)
    if not cites:
        return []
    doc_id = cites[0].doc_id
    # P0-2 scope 隔离：整文档补全只取该文档**且属于当前库**的切片；
    # kb_id 未指定时用命中文档自身所在库兜底（补全不跨库）。
    doc_kb_id = cites[0].kb_id if kb_id is None else kb_id
    # P0-8 active 过滤：只取 active 版本切片（retired 不可补全）
    active_ids = await _active_version_ids(db, kb_id=doc_kb_id, doc_ids=[doc_id])
    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.doc_id == Document.id)
        .where(Chunk.doc_id == doc_id, Chunk.kb_id == doc_kb_id)
    )
    if active_ids:
        stmt = stmt.where(Chunk.document_version_id.in_(active_ids))
    rows = (await db.execute(stmt)).all()
    items: list[RetrievedChunk] = []
    for chunk, doc in rows:
        if len(chunk.content.strip()) < settings.min_content_len:
            continue
        items.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                kb_id=chunk.kb_id,
                doc_id=chunk.doc_id,
                source=doc.filename,
                page=chunk.page,
                section=chunk.section,
                snippet=chunk.content,
                score=0.0,
            )
        )
    items.sort(key=lambda x: x.chunk_id)
    items = items[:cap]
    for i, it in enumerate(items):
        it.rank = i + 1
    return items
