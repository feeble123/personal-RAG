# 单元 R：存储地基（pgvector 迁移）—— 探查 + 待拍板

> 制定日期：2026-08-31
> 前置：单元 P 已收官（严格条款@5 100%），按顺序 O→P→R→S→Q→T 轮到 R
> 性质：**方向性大工程 + 有硬障碍**，须先摆选项让用户拍板，再动手

---

## 一、探查到的事实（连库只读，非猜）

| 项目 | 现状 | 说明 |
|---|---|---|
| 生产库 | **PostgreSQL 17.11**（postgresql+asyncpg） | 早已切 PG，非 SQLite |
| pgvector 扩展 | **未安装** | `pg_available_extensions` 里没有 `vector`（只有 cube/pg_trgm） |
| 当前角色 | `rag_app`，**非 superuser** | 装扩展需 superuser 或 trusted 标记 |
| 向量规模 | **active chunk 2365 个** | 规模很小，Chroma 完全够用 |
| 历史 chunk | 22302 个（含 retired） | 重灌残留，不进索引 |
| embedding 缓存 | 14155 行（vector_json） | **迁移可复用，不用重调 embedding API** |
| 测试库 | 独立临时 SQLite | pgvector 只在 PG 可用，测试需 mock/跳过 |

## 二、两个硬障碍（必须用户知情）

1. **Windows 装 pgvector 需要编译**：pgvector 官方无 Windows 预编译包，需 Visual Studio C++ + PostgreSQL 开发头文件 + nmake 编译。对非程序员用户门槛高，且要动 PG 服务器。
2. **rag_app 非 superuser**：装扩展要么用超级用户（postgres）执行 `CREATE EXTENSION vector`，要么依赖 pgvector ≥0.7.0 的 trusted 标记（前提是扩展文件已装到服务器）。

## 三、价值重估（不替您做主，如实说）

pgvector 的收益是「向量与 chunk 同库同事务、少一个 Chroma 组件、备份更简单」。但：

- 当前 active chunk 仅 **2365 个**，Chroma 单机完全够用，性能不是瓶颈；
- 「漂移」风险已被 P0-8 的 shadow 索引 + count 核对控制住（索引失败旧版不动）；
- 真正的痛点（找得到、答得对）在检索/解析层，不在存储层。

**结论**：单元 R 是「锦上添花」而非「雪中送炭」，且当前有编译障碍。建议**暂缓**，先做能直接提升问答质量的单元 S（解析器选型）/ Q（解析完整性）。

## 四、三个选项（请您拍板）

### 选项 A：暂缓单元 R，改做单元 S（bake-off）或 Q（解析完整性）
- 把力气花在「答得对」上，pgvector 等规模上来/换 Linux 环境再回。
- 成本：0；收益：问答质量直接提升。

### 选项 B：现在装 pgvector 并完成完整迁移
- 需您配合装编译环境（Visual Studio）或用 postgres 超级用户装扩展，然后我按「建表→双写→对比→灰度」逐步做。
- 成本：高（编译 + 改接口 + 双写 + 灰度）；收益：存储层更整。

### 选项 C：先做单元 R 的「纯代码」准备（离线能做），真切换以后再说
- 改 vector_store 抽象接口 + 写 pgvector 后端 + 迁移脚本 + SQLite mock 单测，但 PG 不实际装扩展、不切换。
- 代码先就绪，环境允许时一键启用；成本：中；收益：不阻塞，未来零返工。

## 五、红线（不变）

- 只存 `feature/rag-optimization`，main 不动，不 push
- 碰真实数据（PG/Chroma）先备份，不自动删
- 装扩展/改 PG 需您知情同意，我不擅自动 PG 服务器

---

## 六、决策（2026-08-31）

用户拍板 **选项 A**：暂缓单元 R，改做单元 S（解析器 bake-off）。

- 单元 R 状态：**挂起**，等数据量上来或换 Linux 环境再回（本文件留作当时的启动底稿）。
- 下一个执行单元：**单元 S（解析器 bake-off）**。

