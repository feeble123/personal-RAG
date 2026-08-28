"""追问改写（BUG-追问引用错位）：短/指代性追问合并上一轮用户问题，让新问题自行检索到同主题切片。

背景：多轮对话中用户追问（如「可以以表格的形式来呈现吗」「帮我总结一下」「那第X条呢」）
本身不携带领域主题，直接检索会命中无关切片；而 LLM 会转而抄对话历史答案并沿用历史编号，
导致「答案对但下方数据来源明显不对应」。

改写后用「上一轮问题 + 本轮追问」作为检索查询，保证新问题仍基于真实切片数据回答。
仅影响检索（memory / 语义缓存 / 展示用 prompt 仍用原始问题），不改变用户可见的提问。
"""
from __future__ import annotations

import re

# 指代/追问触发词：命中且句子较短 → 判定为需要合并上一轮问题的追问
_REFERENCE = re.compile(
    r"(这个|上述|上面|以上|该表|该专家|该名单|它|这些|那些|换个|用表格|表格形式|以表格|"
    r"列举|总结|归纳|概括|详细|简洁|继续|然后|还有|为什么|怎么看|怎么理解|翻译|换个说法|换个角度|"
    r"可以吗|能不能|可否|呢)"
)
# 短句阈值：超过该长度视为自带主题的独立问题，不强行合并
_SHORT_LEN = 24
# 明确主题锚点：问句点名文档结构级编号（例题/习题/公式/章节号），自带可检索主题，
# 不是需借上一轮主题的追问。
# 例「例5.6为我详细讲解一下这道题」——「详细」虽命中追问触发词，但「例5.6」已点明主题，
#   合并上一轮（如「公式(9.95)」）会把旧主题带进检索，例5.6 反而找不到。
# 注意**不含条款号（第X条）**：条款是清单子项，常是「那第X条呢」式指代追问，不能误伤。
_EXPLICIT_ANCHOR = re.compile(
    r"(?:例题|习题|例)\s*\d"
    r"|公式\s*[（(]?\s*\d"
    r"|第\s*[0-9一二三四五六七八九十]+\s*[章节篇]"
)
# 纯问候/社交语：不做改写（避免跟在追问后误合并）
_SOCIAL = re.compile(r"^(你好|您好|嗨|哈喽|在吗|谢谢|感谢|再见|拜拜|打扰了|早上好|下午好|晚上好)$")


def needs_followup_rewrite(query: str) -> bool:
    """判断当前问题是否为需要追问改写的短/指代性提问。"""
    q = query.strip()
    if not q:
        return False
    if _SOCIAL.match(q):
        return False  # 纯问候不改写
    if _EXPLICIT_ANCHOR.search(q):
        return False  # 点名例题/习题/公式/章节号 → 自带主题的独立问题，不改写
    if len(q) <= 4:
        return True  # 过短（如「还有呢」「那」）
    if len(q) <= _SHORT_LEN and _REFERENCE.search(q):
        return True
    return False


def rewrite_followup_query(query: str, prev_user_question: str | None) -> str:
    """追问改写：返回新的检索查询（原始问题 + 追问合并），无上一轮/非追问时原样返回。"""
    if not needs_followup_rewrite(query) or not prev_user_question:
        return query
    prev = prev_user_question.strip().strip("？?。！!，,；; \t")
    if not prev:
        return query
    return f"{prev} {query.strip()}"
