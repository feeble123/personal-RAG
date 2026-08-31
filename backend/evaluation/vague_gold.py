"""单元 L：模糊问题 gold 子集（独立于 gold_data.py 的 60 问，互不干扰）。

背景：现有 gold 60 问覆盖 7 种意图，但**没有任何一条是「单轮模糊提问」**。
本文件专门造 11 问模糊题，测「问法不清时，现有向量+BM25+RRF+rerank 召回还能不能命中」。

三类模糊（A 4 问 / B 4 问 / C 3 问，答案均已在库里验证真实存在）：
- A 隐式指代/省略主语：用「最高级别」「平稳还是乱」这类指代/口语，不点明术语
- B 词过泛/缺主语：一个宽泛领域词（「溢洪道」「泵」），答案分散在具体章节
- C 口语换说法：口语词与规范术语字面不匹配（「垮了」vs「溃坝」）

每条的 note 注明「口语→术语」的映射，供人工核对与后续解释。
判定复用 evaluation/scorers.py 的 recall_at_k / recall_clause_at_k，规则与 60 问一致。
"""

VAGUE: list[dict] = [
    # ==================== A 隐式指代/省略主语 ====================
    {
        "q": "什么情况下要启动最高级别的响应？",
        "kb": "重庆市防汛抗旱应急预案",
        "expect_keywords": ["Ⅰ级", "启动条件"],
        "expect_clauses": ["4.3"],
        "answer_hint": "Ⅰ级应急响应启动条件：暴雨红色预警或水情红色预警，经市防指研判",
        "intent": "general",
        "vague_type": "A",
        "note": "「最高级别」=Ⅰ级（隐式指代）。对应 60 问「Ⅰ级应急响应的启动条件是什么？」的模糊版",
    },
    {
        "q": "怎么判断水流是平稳的还是乱的？",
        "kb": "水力学第5版",
        "expect_keywords": ["层流", "雷诺数", "紊流"],
        "expect_clauses": ["4.4"],
        "answer_hint": "用雷诺数判别流态：层流/紊流",
        "intent": "general",
        "vague_type": "A",
        "note": "「平稳还是乱」=层流/紊流，判据是雷诺数（隐式指代，不点明术语）",
    },
    {
        "q": "水在管子里流，什么时候可以直接按长管算？",
        "kb": "水力学第5版",
        "expect_keywords": ["长管", "沿程水头损失"],
        "expect_clauses": ["5"],
        "answer_hint": "局部损失和流速水头占比很小时可按长管计算",
        "intent": "general",
        "vague_type": "A",
        "note": "省略主语「有压管道」。对应 60 问「有压管道按长管计算的依据是什么？」的模糊版",
    },
    {
        "q": "山坡要滑坡了，打哪种桩能挡住？",
        "kb": "GB 38509-2020 滑坡防治设计规范",
        "expect_keywords": ["抗滑桩"],
        "expect_clauses": ["10"],
        "answer_hint": "抗滑桩工程设计，见 10 抗滑桩工程",
        "intent": "general",
        "vague_type": "A",
        "note": "「打桩挡滑坡」→「抗滑桩」（隐式指代，不点明术语）",
    },

    # ==================== B 词过泛/缺主语 ====================
    {
        "q": "溢洪道怎么设计？",
        "kb": "水力学第5版",
        "expect_keywords": ["堰流", "溢洪道"],
        "expect_clauses": ["7"],
        "answer_hint": "溢洪道设计见 7 堰流及闸孔出流",
        "intent": "general",
        "vague_type": "B",
        "note": "「溢洪道设计」宽泛，答案分散在堰流章，需定位 ch7",
    },
    {
        "q": "管道要埋多深才合适？",
        "kb": "GB 50332给水排水工程管道结构设计规范",
        "expect_keywords": ["埋深", "管道"],
        "expect_clauses": None,
        "answer_hint": "管道埋深要求见规范正文",
        "intent": "general",
        "vague_type": "B",
        "note": "「埋深」全库仅 3 块，宽泛问法需精确定位到埋深章节",
    },
    {
        "q": "农村供水的工程要建哪些东西？",
        "kb": "数字孪生农村供水工程建设技术指南（试行）",
        "expect_keywords": ["水源地", "泵站", "水厂"],
        "expect_clauses": ["2.2"],
        "answer_hint": "系统组成：水源地、泵站、输配水管网、水厂、用户终端",
        "intent": "enumeration",
        "vague_type": "B",
        "note": "宽泛「建哪些东西」→ 系统组成（水源地/泵站/水厂）",
    },
    {
        "q": "防滑坡主要要做哪些工程？",
        "kb": "GB 38509-2020 滑坡防治设计规范",
        "expect_keywords": ["排水", "抗滑桩"],
        "expect_clauses": None,
        "answer_hint": "防治工程含排水工程、抗滑桩工程等",
        "intent": "enumeration",
        "vague_type": "B",
        "note": "宽泛「做哪些工程」→ 排水工程/抗滑桩工程（60 问「滑坡防治中排水工程」的模糊版）",
    },

    # ==================== C 口语换说法（字面不匹配） ====================
    {
        "q": "水在管道拐弯的地方为什么会白白损失能量？",
        "kb": "水力学第5版",
        "expect_keywords": ["局部水头损失"],
        "expect_clauses": ["4"],
        "answer_hint": "发生在局部范围内的能量损失叫局部水头损失",
        "intent": "general",
        "vague_type": "C",
        "note": "口语「拐弯损失能量」→术语「局部水头损失」",
    },
    {
        "q": "山坡上的土石要往下滑，怎么看它稳不稳？",
        "kb": "GB 38509-2020 滑坡防治设计规范",
        "expect_keywords": ["稳定性"],
        "expect_clauses": ["7"],
        "answer_hint": "滑坡稳定性验算见规范正文",
        "intent": "general",
        "vague_type": "C",
        "note": "口语「稳不稳」→「稳定性验算」",
    },
    {
        "q": "水流突然由急变缓，水面会鼓起一道波，这是什么现象？",
        "kb": "水力学第5版",
        "expect_keywords": ["水跃"],
        "expect_clauses": ["6.4"],
        "answer_hint": "水跃现象，见堰流/明渠相关章",
        "intent": "general",
        "vague_type": "C",
        "note": "口语描述水跃现象→术语「水跃」",
    },
]
