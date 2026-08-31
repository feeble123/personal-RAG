"""口语→术语 查询扩展（单元 N）：把用户口语/宽泛表达映射为规范术语，检索前追加。

背景：单元 M 收紧评测后，模糊题严格 Recall@5=77.8%。三个失败点根因定位：
「要建哪些东西」连不到「系统组成」、「怎么看它稳不稳」rerank 因聚焦词缺失灭掉正确块、
「什么时候按长管算」定义块召回层没捞进候选池。

思路（**append 不 replace**）：命中口语词时，把规范术语**追加**到原查询末尾——
加词是「给信号」，删词是「丢信息」；映射错最多多一个无关词，不会把能命中的问法搞丢。
只在检索查询生效（向量/BM25/rerank），不影响语义缓存/记忆库/用户可见提问。

映射表设计原则：
- 触发词取**多字短语**（不用单字），降低误伤；
- 歧义词（如「垮了」）加**上下文守卫**（同句含坝/堤/堰/库才映射「溃坝」）；
- 命中即追加、不命中原样返回；表可增长（发现新口语词加一行即可）。
"""
from __future__ import annotations

import re

# 口语/宽泛表达 → 规范术语。key 是正则（在整句里 findall/search 匹配触发），
# value 是追加到查询末尾的术语（空格分隔多个）。
_RULES: list[tuple[re.Pattern, str]] = [
    # 稳不稳 → 稳定性（#2 土石下滑怎么看它稳不稳）
    (re.compile(r"稳不稳|稳不稳定|稳吗"), "稳定性"),
    # 建哪些东西 → 系统组成（#1 农村供水要建哪些东西）
    (re.compile(r"建哪些|要建哪些|有哪些东西|包含哪些东西"), "系统组成"),
    # 按长管算 → 长管计算依据（#3 什么时候按长管算）
    (re.compile(r"按长管算|长管计算|按长管计算"), "长管计算依据"),
    # 平稳还是乱 → 层流 紊流（口语「平稳还是乱」→ 流态术语）
    (re.compile(r"平稳还是乱|水流平稳|平稳的"), "层流 紊流"),
    # 垮了 → 溃坝（歧义：只在句含坝/堤/堰/库时映射，防「桥垮了」误加）
    (re.compile(r"垮了|垮掉|垮塌"), "溃坝"),
]

# 「垮了」类需要上下文守卫的触发词 → 同句必须含这些字才追加
_GUARD_REQUIRED: set[str] = {"溃坝"}
_GUARD_CHARS = "坝堤堰库闸"


def expand_query(query: str) -> str:
    """返回检索用扩展查询：命中口语词时追加规范术语，否则原样返回。

    纯函数、无副作用、无 IO，失败静默回退原查询（绝不抛异常）。
    """
    if not query or not query.strip():
        return query

    added: list[str] = []
    for pattern, term in _RULES:
        if term in added:
            continue  # 已追加过（不同 pattern 可能重叠）
        if not pattern.search(query):
            continue
        # 上下文守卫：歧义词（如「溃坝」）要求同句出现守卫字
        if term in _GUARD_REQUIRED and not any(c in query for c in _GUARD_CHARS):
            continue
        added.append(term)

    if not added:
        return query
    return f"{query.strip()} {' '.join(added)}"
