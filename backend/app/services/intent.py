"""查询意图粗分类（U3 优化）：识别「实时/外部信息」类问题，用于证据等级拒答的动态放行。

策略：只对**实时/外部信息**类问题（天气/时间/日期/新闻/汇率/股票/比分等，静态知识库 + LLM
无此能力）在 KB 无强证据时拒答；问候/闲聊/能力咨询/规范概述/领域问答一律放行，由 LLM
依据证据等级诚实作答（不强行引用弱相关资料）。核心原则：**知识库有内容就必须放行**。
"""
from __future__ import annotations

import re

# 外部实时数据的强触发词（出现即视为实时/外部类）
_EXTERNAL_STRONG = re.compile(
    r"实时|最新消息|头条|新闻|汇率|股票|股价|大盘|涨停|跌停|彩票|开奖|比分|赛事|球赛|股指"
)

# 天气词（须与时间上下文词联合，避免误伤领域词汇如「温度应力」「降雨强度等级」）
_WEATHER_WORD = re.compile(r"天气|气温|温度|降雨|降水|下雨|下雪|多少度|气候")
_TIME_CTX = re.compile(r"今天|明天|后天|现在|当前|未来|此刻|预报|几点")

# 明确的时钟/日历问句（不用裸「现在/当前」，避免误伤「现在这个水库能蓄多少水」类领域问题）
_CLOCK_CAL = re.compile(
    r"几点|几点钟|几点几分|现在几点了|当前时间|现在时间|今天是几|几月几号|星期几|今天日期|现在日期|几号"
)


def is_real_time_query(query: str) -> bool:
    """判断问题是否询问实时/外部信息（系统无此能力，需在证据不强时拒答）。"""
    q = query.strip()
    if not q:
        return False
    # 1) 外部数据强词：实时新闻/汇率/股票/比分等
    if _EXTERNAL_STRONG.search(q):
        return True
    # 2) 天气预报类：时间上下文 + 天气词
    if _WEATHER_WORD.search(q) and _TIME_CTX.search(q):
        return True
    # 3) 明确的时钟/日历问句（现在几点/今天几号/当前时间）
    if _CLOCK_CAL.search(q):
        return True
    return False
