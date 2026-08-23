# P1 检索严谨化 + 回答质量评测 详细实施计划

> 制定日期：2026-08-23
> 前置：P1 单元1-5 完成（R@5=95%、R@10=100% 但仅 20 问单次）
> 用户反馈：①当前评测"一轮简单测试"不够严谨 ②"找得到"和"答得对"都重要，先找得到再答得对 ③要整体能力提升，不是个别问答 ④单元6 收尾延后
> 本文档：把检索评测严谨化（工作包A）+ 回答质量评测（工作包B）细化到文件/函数/指标/验收

---

## 现状缺陷（已查证）

| 缺陷 | 现状 | 影响 |
|---|---|---|
| 样本小 | 20 问，覆盖 12 库中的部分 | 偶然性高，不暴露结构短板 |
| 单次运行 | 无重复，无方差 | 一次运气好≠稳定 |
| 只看召回 | 只测"找没找到"，不测"答没答对" | 检索满分≠回答质量好 |
| gold 宽松 | `expect_keywords` 任一关键词命中即过 | 掩盖真实召回错误 |
| 无分层 | 只有 aggregate + by_intent | 不暴露按库/文档类型的短板 |

**已有可复用能力**：verify.py 的 `verify_completeness`（完备性）+ `verify_citations`（引用忠实）已实现并接入 qa 路由（`answer_verify_enabled` 控制，默认关）。

---

## 工作包A：检索层严谨化（先做，目标"找得到"测扎实）

### A1 扩大 gold 集（20 → 50+ 问）

**改 `evaluation/gold_data.py`**：
- 从 20 问扩到 **50+ 问**，覆盖：
  - 全部 12 个库（每个库 ≥3 问）
  - 全部意图：precise（精确条款）/ general（一般语义）/ named_doc（点名文档）/ enumeration（枚举）/ comparison（比较）/ followup（多轮追问）/ table（表格查询）
- 每问加 `expect_clauses: list[str] | None`（预期条款号，如 ["5.1", "5.2"]）——严格判定的依据
- 每问加 `answer_hint: str | None`（该问的答案要点，供回答质量评测对照）
- **gold 来源标注**：每问记录 `note`（人工核对过的答案出处章节）

### A2 多轮重复 + 方差

**改 `evaluation/run_eval.py`**：
- 加 `--rounds N` 参数（默认 1；严谨模式跑 3 次）
- 每问跑 N 次，报告 **均值 + 标准差**（R@5/R@10 的 mean±std）
- `report.json` 存 `rounds` + `per_round` 明细

### A3 分层报告

**改 `evaluation/scorers.py` + `run_eval.py`**：
- `aggregate` 增加分层：按 **intent / kb / doc_type**（textbook/standard/manual/other）分组
- 报告输出：
  ```
  分意图:   general n=20 R@5=.. R@10=..
           enumeration n=15 ...
  分库:    水力学第5版 n=10 ...
  分文档类型: textbook n=...
  ```
- 暴露"哪个意图/库/类型弱"→ 针对性提升

### A4 gold 严格化

**改 `evaluation/scorers.py`**：
- 加 `recall_strict(cites, expect_clauses)`：**必须命中指定条款号**（`clause_no` 或 section 含条款号）才算过
- 每问同时报宽松（keywords）+ 严格（clauses）两个指标
- **目标**：严格模式 R@5 ≥ 80%（宽松模式当前 95%）

### A5 检索提升（针对 A1-A4 暴露的短板）

**改 `rag.py` / `config.py`**（预期改动，以评测发现为准）：
- `top_k_vector` / `top_k_bm25` 从 50 扩到 100（召回更大，rerank/扩展兜底）
- `top_k_final` 从 5 → 7（最终引用稍多，覆盖更全）
- `complete_expansion_cap` 从 40 → 60（枚举长列表）
- **分意图参数**：precise 类小 top_k，enumeration 类大扩展——query planner 轻量版
- 每改一个参数 → 跑评测对比，**记录改动前后 delta**

### A6 验收
- gold ≥50 问，覆盖 12 库 + 7 意图
- 多轮均值 R@5 ≥ 90%、R@10 ≥ 95%（宽松）
- 严格模式 R@5 ≥ 80%
- baseline.json 更新 + 提交

---

## 工作包B：回答质量评测（后做，目标"答得对"可衡量）

### B1 批量回答评测脚本

**新增 `evaluation/answer_eval.py`**：
- 对 gold 集的问（用 `answer_hint`），调真实 LLM（中转站 deepseek-v4-flash）生成回答
- 流程：
  1. `rag.retrieve(q, kb_id)` → cites
  2. 组装 prompt（复用 `chat.build_prompt`）→ `build_chat_model().ainvoke` 生成 answer
  3. `verify.verify_completeness(q, answer, cites)` → 完备性判定
  4. `verify.verify_citations(answer, cites)` → 引用忠实判定
  5. 记录：answer、完备性 verdict、引用 verdict、cites 数
- 输出 `answer_report.json`：**完备率**（enumeration 题完整比例）、**引用准确率**（无无效引用比例）
- CLI：`python -m evaluation.answer_eval --sample N`

### B2 指标定义
| 指标 | 定义 | 判定 |
|---|---|---|
| 引用准确率 | 回答中 [n] 编号被对应资料支撑的比例 | verify_citations |
| 完备率 | 枚举类回答完整覆盖资料条目的比例 | verify_completeness |
| 事实正确率 | 回答核心事实与 answer_hint 一致 | LLM 判定 + 抽样人工复核 |
| 检索→回答漏斗 | 检索命中的问题中，回答质量达标的比例 | 分层统计 |

### B3 回答编排改进（基于 B1 发现）
- `answer_verify_enabled` 默认开（现默认关）——每次问答生成后自动校验
- 引用校验失败 → 自动重生成（现 `answer_verify_max_retries=2` 已有）

### B4 验收
- answer_eval 可跑（≥30 问样本）
- 报告出引用准确率/完备率/事实正确率
- 发现 ≥2 个回答质量短板并改进

---

## 实施顺序（每单元确认后执行）

| 步骤 | 内容 | 产出 |
|---|---|---|
| A1 | 扩大 gold 到 50+ 问（覆盖 12 库 + 7 意图 + expect_clauses + answer_hint） | gold_data.py |
| A2 | 多轮重复 + 方差报告 | run_eval.py --rounds |
| A3 | 分层报告（intent/kb/doc_type） | scorers.py + run_eval.py |
| A4 | 严格判定（clause 命中） | scorers.py |
| A5 | 检索参数调优（按评测发现） | rag.py/config.py |
| **A 收尾** | A 全量回归 + 提交 + baseline 更新 | — |
| B1 | answer_eval.py 批量回答评测 | 新脚本 |
| B2-B3 | 指标 + 编排改进（answer_verify 默认开） | qa/routes.py |
| **B 收尾** | 全量回归 + 提交 + 单元6 收尾 | — |

## 回滚
- A：参数调优全部可调回（config），gold 扩大是纯新增
- B：answer_verify 默认开可改回关；answer_eval 是独立脚本不影响线上

## 风险
- 扩大 gold 后严格模式可能暴露真实短板（R@5 下降）——这是**好事**（发现真问题），对照 baseline 逐项改进
- 回答评测用真实 LLM → 有成本（deepseek-v4-flash 便宜），`--sample` 控制规模
- 多轮跑 3 次 × 50 问 × 真实 embedding → 耗时（可接受，评测非高频）

## 关键决策
1. **先 A 后 B**：检索（找得到）是回答（答得对）的前提，A 的严格评测先立好
2. **宽松+严格双指标**：宽松反映召回能力，严格反映精准能力，都不丢
3. **多轮方差**：不是"跑一次看运气"，而是"跑多次看稳定"
4. **分层暴露短板**：总分掩盖结构问题，分层才看得到"哪个库/意图弱"
