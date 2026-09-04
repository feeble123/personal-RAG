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

# 水情/水位监控强触发（出现即视为实时监控类：当前/今日/实时/最新 + 监控对象）
_WATER_STRONG = re.compile(
    r"实时水位|当前水位|今日水位|最新水位|实时水情|当前水情|今日水情|最新水情|"
    r"实时雨量|今日雨量|当前雨量|当前流量|实时流量|今日流量|闸门开度|闸门状态|"
    r"闸门启闭|水位变化|水情变化|水势如何"
)

# 水情监控词（须与时间上下文词联合，避免误伤设计值：「正常蓄水位」无时间词 → 不触发）
_WATER_WORD = re.compile(r"水位|流量|蓄水量|雨量|水情|雨情|汛情|闸门|水势")

# 设计值保护：即便带时间词（如「当前正常蓄水位」），设计/特征水位仍属知识库可答，不判实时
_DESIGN_WATER = re.compile(
    r"正常蓄水位|设计洪水位|校核洪水位|汛期限制水位|防洪水位|设计水位|运行水位|"
    r"死水位|兴利库容|设计流量|特征水位|最低水位|最高水位|多年平均|保证率"
)


def is_real_time_query(query: str) -> bool:
    """判断问题是否询问实时/外部信息（系统无此能力，需在证据不强时拒答）。

    水情监控类（当前/今日水位、水情、雨量、闸门状态等）识别为实时：静态规范里的
    设计蓄水位 ≠ 今日实时水位，无强证据时拒答防拿设计值冒充实时值（P0-4）。
    设计值（正常蓄水位/设计洪水位/汛期限制水位等）即使带时间词也不判实时。
    """
    q = query.strip()
    if not q:
        return False
    # 0) 设计/特征水位保护：知识库可答，即使带「当前/今天」也不判实时
    if _DESIGN_WATER.search(q):
        return False
    # 1) 外部数据强词：实时新闻/汇率/股票/比分等
    if _EXTERNAL_STRONG.search(q):
        return True
    # 2) 水情监控强触发：当前/今日/实时水位、水情、闸门状态等
    if _WATER_STRONG.search(q):
        return True
    # 3) 水情监控词 + 时间上下文：今天水位 / 当前蓄水量 / 此刻汛情
    if _WATER_WORD.search(q) and _TIME_CTX.search(q):
        return True
    # 4) 天气预报类：时间上下文 + 天气词
    if _WEATHER_WORD.search(q) and _TIME_CTX.search(q):
        return True
    # 5) 明确的时钟/日历问句（现在几点/今天几号/当前时间）
    if _CLOCK_CAL.search(q):
        return True
    return False


# ---- 表格结构化查询信号（单元二 2-4）----
# 计数类问法：X 的数量/多少/几台/几套…——答案落在一张表的某列某值，应精确读。
_TABLE_COUNT_RE = re.compile(r"数量|多少|几台|几套|几座|几个|几处|个数|台数|套数|共计|合计")
# 枚举类问法：列出/有哪些/清单/一览…——答案是一张表里某列的整列值。
_TABLE_ENUM_RE = re.compile(r"有哪些|列出|清单|一览|名单|哪些|所有|全部|都有|都包含")


def table_query_kind(query: str) -> str | None:
    """判断问题是否「读表」式，并返回子类型：`count`（计数/查值）/ `enum`（枚举/清单）。

    这是**宽松预筛**：只要出现计数/枚举信号就进入精确通道候选。真正的门禁在
    精确通道内部——找不到强匹配的表（或无法精确读出答案）就返回 None 回退向量检索，
    保证「预警分级有哪几级」「应急预案有哪些措施」这类非表格问题零回归。
    返回 None 表示非读表式问题。
    """
    q = query.strip()
    if not q:
        return None
    if _TABLE_COUNT_RE.search(q):
        return "count"
    if _TABLE_ENUM_RE.search(q):
        return "enum"
    return None


def is_table_query(query: str) -> bool:
    """判断问题是否「读表」式（计数 / 枚举 / 查值）。等价于 table_query_kind(q) is not None。"""
    return table_query_kind(query) is not None
