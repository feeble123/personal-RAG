"""rerank 聚焦主题词提取（G6-2 从 rag.py 抽出）。

BGE 系列模型对长查询会稀释关键项：整句拿去 rerank 时，文档标题/疑问词会喧宾夺主，
正确规则节反而排到前言/总则之后。这里提取「核心主题词」只用于 rerank 打分，
向量/BM25 召回仍用原查询（保证候选池不变）。
"""
from __future__ import annotations

import re

# 中文问句的提问词（用于提取 rerank 核心主题词）
# 单元 C 方向1：补「是多少」「分为」「有几级/几种/几类」等此前未识别的问法——
# 否则整句原样拿去 rerank，被「哪几个」「是多少」等噪声稀释（实测「预警信息分为哪几个等级」
# 整句 0.046，聚焦「预警信息」后可命中「3.4 预警分级」）。
_QUESTION_PATTERN = re.compile(
    r"(.{2,30}?)(?:是什么|是多少|有哪些|有几级|有几种|有几类|分为|有什么要求|有什么规定|"
    r"有何要求|包括哪些|包含哪些|指什么|是什么样的|如何)"
)
_QUESTION_PRE = re.compile(r"^(请问|你好|帮我|我想知道|麻烦问下|想了解下|麻烦查下)+")
# 泛词：切分后跳过（如「关于公式的要求」不能取末尾的「要求」）
# 单元 C 方向1：补入「关系词」——「X的定义/依据/步骤/数量/等级」的 X 才是检索核心，
# 单独取「定义/依据/构建」当聚焦词是噪声（实测「水力半径的定义」只剩「定义」→ 搜不到「水力半径」）。
# 这类词作为「X的{词}」末尾时跳过，从后往前取到真正主体 X。
_GENERIC_WORDS = {
    "要求", "规定", "内容", "情况", "方法", "原则", "事项", "问题", "标准", "条款", "方面", "部分",
    "定义", "依据", "步骤", "目的", "意义", "作用", "职责", "原理", "数量", "等级", "类别", "计算", "构建",
}
# 泛词前的修饰前缀：「具体要求」「主要规定」仍是泛词，须跳过（否则「组织指挥体系的具体要求」
# 会取「具体要求」当主题词，rerank 全排成噪声；「引用标准」的「引用」不是修饰词，不受影响）
_GENERIC_MODIFIERS = ("具体", "主要", "基本", "相关", "一般", "重要", "相应", "有关", "详细")
# 动词前缀（「引用标准的编写要求」里「编写要求」是追问内容，主体是「引用标准」，
# 不能取「编写要求」当主题词——否则 reranker 搜不到「3.2 引用标准」章节）。
_GENERIC_VERB_PREFIX = ("编写", "编制")


def _is_generic_part(part: str) -> bool:
    """段是否为泛词：精确匹配，或「修饰词/动词+泛词」（具体/主要/基本…要求/规定…，
    编写/编制…要求/格式…）。「编写要求」是追问内容，应跳过取前面的主体词。"""
    if part in _GENERIC_WORDS:
        return True
    prefix = rf"^(?:{'|'.join(_GENERIC_MODIFIERS + _GENERIC_VERB_PREFIX)})"
    base = re.sub(prefix, "", part)
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
    # 等级量词信号：问「分几级/有几级」时，答案通常落在「XX分级」章节（如 4.1 响应分级、
    # 3.4 预警分级）。focus 只留主体词会丢量词，reranker 排不到「分级」块，反被「响应措施」
    # 等长块压过。subj 不含「级」时追加「分级」补回信号（只加词不改词）。
    # 放在「太长回退」守卫之后，避免追加后变长误触发回退。
    if re.search(r"分几级|有几级|分为几级|几级|分级|等级", s) and "级" not in subj:
        subj = f"{subj} 分级"
    return subj
