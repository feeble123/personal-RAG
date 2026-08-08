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


async def resolve_documents_by_title(db: AsyncSession, query: str) -> list[int]:
    """从问题中提取书名/文档名（《…》或「XXX中」），匹配知识库文档，返回 doc_id 列表。

    匹配不到（宽泛问法如「在数字孪生工程中…」、泛词如「标准中规定…」）返回空列表，
    检索回退跨库。支持多书名（对比两份规范 → 返回多个 doc_id）。
    """
    from app.db.models import Document

    docs = (await db.scalars(select(Document))).all()
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


# 完整性意图：查询要求完整/所有/全部/清单等枚举类回答（触发列表章节全量扩展）
_COMPLETENESS_RE = re.compile(
    r"完整|所有|全部|清单|一览|全貌|补全|还有|其他|其余|全都|所有的|全部的|都要|都有|只要.*都|只.*(?:吗|？|\?)"
)
# 列表类章节组件：附件/名单/清单/成员/组成/一览/列表
_LIST_COMPONENT_RE = re.compile(r"附件\d|名单|清单|成员|组成|一览|列表")


def _list_section_component(sec: str | None) -> str | None:
    """从章节路径中找最深的「列表类」组件（附件/名单/清单/成员…），找不到返回 None。"""
    if not sec:
        return None
    comps = [p.strip() for p in sec.split("/") if p.strip()]
    for comp in reversed(comps):
        if _LIST_COMPONENT_RE.search(comp):
            return comp
    return None


async def _expand_complete_list(
    db: AsyncSession,
    query: str,
    cand_sorted: list[tuple[int, float]],
    section_by_id: dict[int, str | None],
    top_k: int,
    candidates_full: dict[int, float] | None = None,
) -> list[tuple[int, float]] | None:
    """完整性扩展：查询要求「完整/所有/全部/清单」时，纳入相关列表章节的**全部切片**
    （直接归属、排除嵌套附件），保证枚举/清单类回答不遗漏任何成员。

    背景：专家名单等多页列表类章节，top_k=5 只覆盖部分页面 → 模型「每次漏一部分答案」。
    扩展上限 complete_expansion_cap（默认 40），足以覆盖几十条的完整列表。
    """
    if not cand_sorted:
        return None
    if not _COMPLETENESS_RE.search(query):
        return None
    # 在整个候选池中找「列表类章节」，选候选分最高者所属章节。
    # 不能只看 top1：自然问法（完整/所有/清单）经 rerank 后 top1 往往不在列表章节，
    # 名单块被挤到候选池深处——须扫描全部候选，再从 DB 拉该章节全部切片。
    best_comp: str | None = None
    best_score = -1.0
    for cid, sc in cand_sorted:
        comp = _list_section_component(section_by_id.get(cid))
        if comp and sc > best_score:
            best_score = sc
            best_comp = comp
    # 放宽：top-100 候选里没有列表块（名单块 fused 分可能排到 100 名外）时，
    # 扫描完整候选集，动态补查章节，确保列表章节不因候选截断而漏检。
    if not best_comp and candidates_full:
        extra_ids = [cid for cid in candidates_full if cid not in section_by_id]
        extra_section: dict[int, str | None] = {}
        if extra_ids:
            rows = (
                await db.execute(
                    select(Chunk.id, Chunk.section).where(Chunk.id.in_(extra_ids))
                )
            ).all()
            extra_section = {cid: sec for cid, sec in rows}
        for cid, sc in candidates_full.items():
            comp = _list_section_component(extra_section.get(cid))
            if comp and sc > best_score:
                best_score = sc
                best_comp = comp
    if not best_comp:
        return None
    rows = (
        await db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.doc_id == Document.id)
            .where(Chunk.section.contains(best_comp))
        )
    ).all()
    members = [(c, d) for c, d in rows if _under_component(c.section, best_comp)]
    members = members[: settings.complete_expansion_cap * 2]  # 超大列表防失控
    if len(members) < 4:
        return None  # 章节块太少（≤3）不扩展，top_k 已覆盖；≥4 则全部纳入防遗漏
    ids = [c.id for c, _ in members]
    if settings.rerank_enabled:
        docs = [c.content for c, _ in members]
        try:
            scores = await rerank(focus_rerank_query(query), docs)
            ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
        except Exception:
            logger.warning("完整性扩展 rerank 失败，按原文顺序", exc_info=True)
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
) -> list[tuple[int, float]] | None:
    """综合型问题：主题词命中章节的多个子节时，把整章子节纳入最终引用。

    规范类章节横跨多子节多页（如重庆预案「5 应急保障」5.1~5.12），top_k=5 只覆盖
    一小部分 → LLM 必然答不完整。按「与聚焦主题词匹配的章节组件」扩展：
    - 命中整章组件（「5 应急保障」）→ 覆盖该章全部子节；
    - 命中窄组件（「3.2 引用标准」）→ 只扩该组件，不扩大章节；
    - 命中子节数不足 top_k → 维持原 top_k。
    直接从 DB 取该组件下全部切片再 rerank 排序（不受 rerank 候选池限制，
    跨库检索时也不会因候选被挤占而丢子节）。返回最多 15 条。
    """
    if not cand_sorted:
        return None
    if not settings.rerank_enabled:
        return None  # 章节扩展依赖 rerank 排序；离线/未开启时跳过，避免无意义网络调用
    top1 = cand_sorted[0][0]
    comp = _match_section_component(focus_rerank_query(query), section_by_id.get(top1))
    if not comp:
        return None
    rows = (
        await db.execute(
            select(Chunk, Document).join(Document, Chunk.doc_id == Document.id)
        )
    ).all()
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
) -> list[RetrievedChunk]:
    """检索主入口。kb_id=None 跨全库（按库归一化 BM25 加权）；doc_ids 限定只搜点名文档。

    final = 0.7×向量相似度 + 0.3×BM25归一化分
    - 向量为主（真实余弦，区分度好）
    - BM25 精确关键词兜底：解决 BGE-M3 对「计算公式/推求步骤」等查询区分度不足，
      以及长问题下关键词被稀释的问题（如「请问…通信网络体系有什么要求呢？」）
    - 跨库时各库 BM25 按**该库自身 top1 归一化**，无关库（BM25 近 0）不会产生噪声
    - doc_ids 非空：只搜这些文档的切片（问题点名《书名》或「XXX中」时），BM25 按文档后过滤
    """
    top_k = top_k or settings.top_k_final

    # 1) 向量检索（真实余弦相似度）；doc_ids 限定 → Chroma metadata doc_id 过滤
    qvec = await embed_query(query)
    doc_chunk_ids: set[int] = set()
    doc_kb_ids: set[int] = set()
    if doc_ids:
        rows = (
            await db.execute(select(Chunk.id, Chunk.kb_id).where(Chunk.doc_id.in_(doc_ids)))
        ).all()
        doc_chunk_ids = {cid for cid, _ in rows}
        doc_kb_ids = {kid for _, kid in rows}
        where = {"doc_id": doc_ids[0]} if len(doc_ids) == 1 else {"doc_id": {"$in": doc_ids}}
    else:
        where = {"kb_id": kb_id} if kb_id else None
    vec_hits = await asyncio.to_thread(vector_store.query, qvec, where, settings.top_k_vector)
    candidates: dict[int, float] = {h.chunk_id: h.score for h in vec_hits}

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

    # 3) BM25 补召回：向量 top50 未出现的 chunk，实时算真实余弦
    extra_ids = [cid for cid in bm25_norm if cid not in candidates]
    if extra_ids:
        embeddings = await asyncio.to_thread(vector_store.get_embeddings_by_ids, extra_ids)
        for cid, vec in embeddings.items():
            candidates[cid] = _cosine(qvec, vec)

    # 4) 加权融合（clamp 到 0~1）
    for cid, sim_v in list(candidates.items()):
        candidates[cid] = max(0.0, 0.7 * sim_v + 0.3 * bm25_norm.get(cid, 0.0))

    # 5) 送入 reranker 重排（cross-encoder，纠正向量对部分查询的区分度不足）
    cand_sorted = sorted(candidates.items(), key=lambda x: x[1], reverse=True)[
        : settings.rerank_candidates
    ]
    rerank_ok = False
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
        docs = [content_by_id.get(cid, "") for cid in ids]
        if settings.rerank_enabled:
            try:
                # 用聚焦主题词重排：长查询会稀释关键项（BGE 局限），
                # 聚焦「引用标准」后正确规则节 0.98+（原文案仅 0.81 被前言压过）
                r_scores = await rerank(focus_rerank_query(query), docs)
                cand_sorted = sorted(zip(ids, r_scores), key=lambda x: x[1], reverse=True)
                rerank_ok = True
            except Exception:
                logger.warning("rerank 失败，回退到混合排序", exc_info=True)
                cand_sorted = cand_sorted[:top_k]

    # 6) 取 top_k；先章节扩展（综合型问题整章覆盖），再完整性扩展（枚举/清单类问题不遗漏）
    ranked = cand_sorted[:top_k]
    expanded = await _expand_chapter_sections(db, query, cand_sorted, section_by_id, top_k)
    if not expanded:
        expanded = await _expand_complete_list(
            db, query, cand_sorted, section_by_id, top_k, candidates_full=candidates
        )
    if expanded:
        ranked = expanded
    return await _hydrate(db, ranked, include_snippet)


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
        # 过滤低信息量短块（封面/目录碎片，向量相似度虚高）
        if len(chunk.content.strip()) < settings.min_content_len:
            continue
        snippet = chunk.content if include_snippet else chunk.content[:200]
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
            )
        )
    items.sort(key=lambda x: order[x.chunk_id])
    for i, it in enumerate(items):
        it.rank = i + 1
    return items
