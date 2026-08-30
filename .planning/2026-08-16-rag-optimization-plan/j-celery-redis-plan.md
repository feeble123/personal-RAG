# 单元 J：Celery + Redis worker 分层（完整版）

> 制定日期：2026-08-30
> 背景：用户拍板「完整上 Celery+Redis」，为未来上线成 App、多机扩展铺路。
> 执行节奏：**分单元，每单元完成停等验收**。

---

## 一、为什么做（大白话）+ 诚实的前提

现在后台任务是「**一台电脑上的一个后台小工人**」（进程内 asyncio worker），它已经干得不错：

- 不会丢活（DB 是任务记录本，重启后继续）
- 不会堵前台（上传秒返回，后台慢慢解析）
- 不会重复干（同一文档已有活就跳过）

那为什么还要上 Celery+Redis？因为「**一个工人」有天花板**：

- 将来 50 人同时传文档，解析/OCR 这种重活会排队；
- 重活（GPU OCR）和轻活（写库）混在一个工人手里，会互相拖累；
- 想加机器时，一个进程内工人没法分到多台机器上。

Celery+Redis = **雇一个「调度中心」（Redis）+ 一群「分工不同的工人」（Celery worker）**：

| 队列（工种） | 干什么 | 并发策略 |
|---|---|---|
| `ingestion.control` | 轻量状态机、领任务 | 高并发 |
| `parser.cpu` | PyMuPDF / Office / CPU OCR | 低并发（防内存爆） |
| `parser.gpu` | MinerU GPU 解析 | 极低并发（防显存爆） |
| `embedding` | 外部 API 打向量 | 高并发（网络等待多） |
| `indexing` | 写 Chroma / BM25 / DB | 中并发（写串行） |
| `maintenance` | GC、评测、备份检查 | 低并发 |

---

## 二、前提环境（需你先拍板，见文末）

查了你的电脑：**Redis 没装、Docker 没装**。要上 Celery+Redis，得先装环境。三个方案：

| 方案 | 说明 | 优劣 |
|---|---|---|
| A. Docker 跑 Redis | 装 Docker Desktop，用容器跑 Redis | 一箭双雕：Docker 本就是单元 K 要装的 |
| B. 装 Memurai | Redis 的 Windows 兼容版（第三方） | 简单直接，但多一个第三方软件 |
| C. WSL2 装 Linux Redis | Windows 自带 Linux 子系统 | 最接近生产，但配置最复杂 |

---

## 三、分 5 个单元

### 单元 1：环境准备

- 按你拍板的方案装 Redis（Docker / Memurai / WSL2）
- 验证 `redis-cli ping` 返回 PONG
- 装 Celery 依赖：`celery>=5.4`、`redis>=5.0`（走阿里云镜像）

**验收**：`redis-cli ping` → PONG；`pip list` 有 celery + redis。

### 单元 2：Celery app + 配置

- 新增 `app/core/celery_app.py`：Celery 实例 + 6 个队列定义 + 每个队列独立并发/timeout
- `.env` 加 `REDIS_URL`（默认 `redis://localhost:6379/0`）
- config.py 加 redis 配置项

**验收**：`celery -A app.core.celery_app inspect ping` 能通。

### 单元 3：任务重构（核心，保留 DB 真相源）

关键原则（P2-2 原计划明确要求）：**Redis 只当「传话的」，任务真相仍在 PostgreSQL**。

- 把现有 `_process_document` 包装成 Celery task，task 只接收 `job_id`
- Celery task 启动时**查 DB 确认 stage**（幂等：重复投递不重复干）
- 现有「CAS 领任务（UPDATE..WHERE stage='queued'）」逻辑保留，作为 Celery 之外的兜底
- async 代码在 task 里用 `asyncio.run()` 包一层（现有 Chroma/embedding 都是 async）

**验收**：上传文档 → Celery worker 处理 → 入库完成；job 状态在 DB 可查。

### 单元 4：幂等 + 重试 + 故障测试

- `acks_late` 只用于已证明幂等的 task（重解析/入库）
- worker 被杀、任务重发 → 不丢、不重复
- 写测试：模拟 worker 中途崩溃 → reaper 回收 → 重跑成功

**验收**：杀死 worker 不丢 job；重发同一 job 不重复入库。

### 单元 5：队列积压监控

- Prometheus 加指标：各队列长度、最老任务排队时长
- `/metrics` 可看到积压；积压超阈值告警
- 上传 API 在积压时返回「排队中」的可解释状态

**验收**：`/metrics` 有队列长度指标；积压时前端能看到排队状态。

---

## 四、风险与诚实的提醒（必读）

1. **Windows 上 Celery 有坑**：Celery 官方对 Windows 支持有限（多进程 pool 有 bug），常用 `--pool=solo` 或 `--pool=threads`。这意味着「多机/多进程」在 Windows 上是打折扣的——**真正的多机扩展要等 Linux 部署（单元 K Docker）才完全兑现**。
2. **引入 Redis = 多一个故障点**：Redis 挂了任务会堵，要监控 + 启动脚本兜底。
3. **架构级重构**：动了 worker 核心。但「DB 真相源 + CAS 幂等」这个地基保留，风险可控。
4. **诚实的价值判断**：50 人规模，单机其实够用。Celery+Redis 的增量价值主要在「多机横向扩展 + 队列资源隔离」，是为**将来**铺路，不是现在性能不够。

---

## 五、已拍板（2026-08-30）

1. **环境方案**：**A（Docker 跑 Redis）**——装 Docker Desktop，用容器跑 Redis。
2. **执行节奏**：**J 和 K 分开做，先 J 后 K**——先单独把 Celery+Redis 在 Windows 本机跑通，Docker 部署（单元 K）留到后面单独推进。

> 据此，单元 1 环境准备按「Docker Desktop 跑 Redis」执行；Docker 部署（应用容器化）不在本单元范围，属后续单元 K。
