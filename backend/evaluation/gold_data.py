"""P1-1 评测门禁：gold 标注集。

每条记录 = 一个可重复验证的检索问题：
- q：用户问法（真实）
- kb：知识库名（必须能解析到 kb_id）
- expect_keywords：检索结果中**至少一个** chunk 应包含的关键词（任一个命中即算过）
- intent：问题意图（精确条款/一般语义/点名文档/枚举/表格），供分意图统计
- note：标注依据（答案来源章节/行，人工核对过）

标注原则：
- 问题来自真实答辩/使用场景，答案在对应库中有明确来源
- expect_keywords 取原文里不易被改写误解的实词（条款号/规范名/专业术语）
"""
from __future__ import annotations

GOLD: list[dict] = [
    # ---- 水利工程基础（库1，md）----
    {
        "q": "明渠均匀流的形成条件是什么？",
        "kb": "水利工程基础",
        "expect_keywords": ["明渠均匀流", "形成条件"],
        "intent": "general",
        "note": "水力学-明渠流动.md 章节",
    },
    {
        "q": "水循环是指什么？",
        "kb": "水利工程基础",
        "expect_keywords": ["水循环", "太阳辐射"],
        "intent": "general",
        "note": "工程水文学-径流与暴雨.md",
    },
    # ---- 数字孪生水利导则（库2，pdf）----
    {
        "q": "数字孪生流域建设的技术要求有哪些？",
        "kb": "数字孪生水利导则",
        "expect_keywords": ["数字孪生", "流域"],
        "intent": "enumeration",
        "note": "导则正文章节",
    },
    {
        "q": "什么是数字孪生？",
        "kb": "数字孪生水利导则",
        "expect_keywords": ["数字孪生", "术语"],
        "intent": "general",
        "note": "3 术语和定义章节",
    },
    # ---- 水利技术标准编写规定（库4，pdf）----
    {
        "q": "水利技术标准编写规定中，引用标准的编写要求是什么？",
        "kb": "水利技术标准编写规定",
        "expect_keywords": ["引用标准", "编写"],
        "intent": "general",
        "note": "SL 1-2014 规则节",
    },
    {
        "q": "标准中公式的编写格式有什么规定？",
        "kb": "水利技术标准编写规定",
        "expect_keywords": ["公式", "编写"],
        "intent": "general",
        "note": "SL 1-2014 公式章节",
    },
    # ---- 重庆市防汛抗旱应急预案（库5，pdf）----
    {
        "q": "重庆市防汛抗旱应急预案的编制目的是什么？",
        "kb": "重庆市防汛抗旱应急预案",
        "expect_keywords": ["编制目的", "水旱灾害", "抗洪抢险"],
        "intent": "general",
        "note": "1 总则/1.1 编制目的",
    },
    {
        "q": "应急预案中的应急响应分为几级？",
        "kb": "重庆市防汛抗旱应急预案",
        "expect_keywords": ["应急响应"],
        "intent": "enumeration",
        "note": "应急响应章节",
    },
    {
        "q": "预案中技术保障有哪些内容？",
        "kb": "重庆市防汛抗旱应急预案",
        "expect_keywords": ["技术保障"],
        "intent": "enumeration",
        "note": "技术保障章节",
    },
    # ---- GB 38509 滑坡防治设计规范（库7，pdf）----
    {
        "q": "滑坡防治设计规范中，滑坡稳定性验算的要求是什么？",
        "kb": "GB 38509-2020 滑坡防治设计规范",
        "expect_keywords": ["滑坡", "稳定性"],
        "intent": "general",
        "note": "规范正文",
    },
    {
        "q": "滑坡防治的勘察要求包括哪些？",
        "kb": "GB 38509-2020 滑坡防治设计规范",
        "expect_keywords": ["勘察"],
        "intent": "enumeration",
        "note": "勘察章节",
    },
    # ---- 已报送方案台账（库9，xlsx）----
    {
        "q": "已报送方案台账中，制度体系有哪些方案？",
        "kb": "已报送方案台账",
        "expect_keywords": ["质量保证体系", "肖家湾水厂", "制度体系"],
        "intent": "enumeration",
        "note": "制度体系 sheet",
    },
    {
        "q": "台账中备案版方案有哪些？",
        "kb": "已报送方案台账",
        "expect_keywords": ["备案版", "方案台账"],
        "intent": "enumeration",
        "note": "备案版方案台账 sheet",
    },
    # ---- GB 50332 管道结构设计规范（库11，pdf）----
    {
        "q": "给水排水管道结构设计规范中，管道埋深的要求是什么？",
        "kb": "GB 50332给水排水工程管道结构设计规范",
        "expect_keywords": ["管道", "埋深"],
        "intent": "general",
        "note": "规范正文",
    },
    # ---- 水力学第5版（库12，OCR pdf，主测试料）----
    {
        "q": "静水压力是指什么？",
        "kb": "水力学第5版",
        "expect_keywords": ["静水压力"],
        "intent": "general",
        "note": "2 水静力学",
    },
    {
        "q": "什么是局部水头损失？",
        "kb": "水力学第5版",
        "expect_keywords": ["局部水头损失"],
        "intent": "general",
        "note": "4 流动阻力与水头损失",
    },
    {
        "q": "明渠流动的断面面积公式是什么？",
        "kb": "水力学第5版",
        "expect_keywords": ["断面", "m", "h"],
        "intent": "general",
        "note": "6 明渠流动",
    },
    {
        "q": "有压管道按长管计算的依据是什么？",
        "kb": "水力学第5版",
        "expect_keywords": ["长管", "沿程水头损失"],
        "intent": "general",
        "note": "5 有压管道流动",
    },
    {
        "q": "堰流的水头包括哪些？",
        "kb": "水力学第5版",
        "expect_keywords": ["堰顶", "水头"],
        "intent": "enumeration",
        "note": "7 堰流及闸孔出流",
    },
    {
        "q": "水力学中水头损失分为哪几类？",
        "kb": "水力学第5版",
        "expect_keywords": ["沿程损失", "局部损失"],
        "intent": "enumeration",
        "note": "4 流动阻力与水头损失",
    },
]
