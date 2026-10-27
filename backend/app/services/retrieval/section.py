"""章节路径匹配（G6-2 从 rag.py 抽出）。

章节扩展/枚举扩展共用的纯函数：编号剥离、组件匹配、直接归属判定。
"""
from __future__ import annotations

import re

# 枚举/概述类问题检测（跨库通用问句，不依赖知识库内容：台账/名单/章节等任何结构都适用）
ENUMERATION_RE = re.compile(
    r"有哪些|包含哪些|包括哪些|都有哪些|哪几个|哪几类|哪几级|哪几种|多少|所有|全部|完整|完整的|列出来|列出|"
    r"概述|介绍|一览|全貌|总结|汇总|清单|一览表|都要|都有|还有|其他|其余|别的"
)


def section_no(comp: str) -> str:
    """去掉章节组件里的编号前缀：「5 应急保障」→「应急保障」，「3.2 引用标准」→「引用标准」。"""
    return re.sub(r"^[\d.]+\.?\s*", "", comp)


def match_section_component(focused: str, section: str | None) -> str | None:
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
        if focused in section_no(comp) or section_no(comp) in focused:
            if i > best_depth:
                best, best_depth = comp, i
    return best


def under_component(sec: str | None, comp: str) -> bool:
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
