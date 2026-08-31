"""Celery 应用实例 + 多队列 worker 分层配置（单元 J）。

设计原则（P2-2 原计划明确要求）：
- Redis 只当「传话的」broker 调度器，任务真相仍在 PostgreSQL（IngestionJob 表）。
  Celery task 只接收 job_id，启动时查 DB 确认 stage（幂等），DB 才是唯一真相源。
- use_celery=false（默认）走进程内 asyncio worker（manager.py），Celery 不启用；
  true 时由 Celery worker 从各队列拉任务处理——双轨并存，可随时切回，互不冲突。
- 队列按「工种」隔离并发：解析/GPU 重活低并发防爆内存显存，打向量高并发吃网络等待。

Windows 注意（计划「风险提醒」）：Celery 官方对 Windows 多进程 pool 支持有限，
本机开发/验证用 `--pool=solo` 或 `--pool=threads`；真正的多机横向扩展需等
Linux 部署（单元 K Docker）才完全兑现。
"""
from __future__ import annotations

from celery import Celery
from kombu import Queue

from app.core.config import settings

# ---- 6 个队列（工种）定义 ----
# 队列名即工种；每队列并发上限取自 settings 的 celery_*_concurrency 配置项。
QUEUE_INGESTION_CONTROL = "ingestion.control"  # 轻量状态机/领任务，高并发
QUEUE_PARSER_CPU = "parser.cpu"                # PyMuPDF / Office / CPU OCR，低并发防内存爆
QUEUE_PARSER_GPU = "parser.gpu"                # MinerU GPU 解析，极低并发防显存爆
QUEUE_EMBEDDING = "embedding"                  # 外部 API 打向量，高并发吃网络等待
QUEUE_INDEXING = "indexing"                    # 写 Chroma / BM25 / DB，中并发（写串行）
QUEUE_MAINTENANCE = "maintenance"              # GC / 评测 / 备份检查，低并发

QUEUE_NAMES: tuple[str, ...] = (
    QUEUE_INGESTION_CONTROL,
    QUEUE_PARSER_CPU,
    QUEUE_PARSER_GPU,
    QUEUE_EMBEDDING,
    QUEUE_INDEXING,
    QUEUE_MAINTENANCE,
)

# 每队列并发上限：映射 settings 的 celery_*_concurrency（模块导入时读取一次）。
# 用途：启动脚本按队列分别起 worker 时用 `-c <并发>`；监控时按队列展示积压。
# 注意 Celery 单个 worker 进程对所有监听队列共用一个并发值——「每队列独立并发」
# 要靠按队列分别起 worker 进程实现，见 docs/P2-DEPLOYMENT.md 的启动脚本。
QUEUE_CONCURRENCY: dict[str, int] = {
    QUEUE_INGESTION_CONTROL: settings.celery_ingestion_concurrency,
    QUEUE_PARSER_CPU: settings.celery_parser_concurrency,
    QUEUE_PARSER_GPU: settings.celery_parser_gpu_concurrency,
    QUEUE_EMBEDDING: settings.celery_embedding_concurrency,
    QUEUE_INDEXING: settings.celery_indexing_concurrency,
    QUEUE_MAINTENANCE: settings.celery_maintenance_concurrency,
}

# 每队列任务软/硬超时（秒）：软超时发 SoftTimeLimitExceeded（可捕获做清理），
# 硬超时直接杀进程。解析/GPU 重活给长超时；轻量控制/写库给短超时。
# 单元③ 在 task_annotations 里按队列落实到具体 task（Celery 超时是 task 级而非队列级）。
QUEUE_TIMEOUTS: dict[str, tuple[int, int]] = {
    QUEUE_INGESTION_CONTROL: (30, 60),
    QUEUE_PARSER_CPU: (600, 900),
    QUEUE_PARSER_GPU: (1800, 3600),   # MinerU 整文档解析可能很久（config mineru_timeout_sec=1800）
    QUEUE_EMBEDDING: (300, 600),
    QUEUE_INDEXING: (300, 600),
    QUEUE_MAINTENANCE: (600, 1200),
}

# ---- Celery 实例 ----
# backend=None：不设 result backend——任务结果真相在 DB（IngestionJob），
# 不额外往 Redis 存结果（省内存、单一真相源）。
celery_app = Celery(
    "rag",
    broker=settings.redis_url,
    backend=None,
)

celery_app.conf.update(
    # 显式声明 6 个队列（routing_key 与队列同名，任务路由时用）
    task_queues=tuple(Queue(name, routing_key=name) for name in QUEUE_NAMES),
    task_default_queue=QUEUE_INDEXING,
    task_default_routing_key=QUEUE_INDEXING,
    # 生产 worker 若监听了一个「定义里没有」的队列，允许自动建（开发容错）
    task_create_missing_queues=True,
    # broker 启动时断连重试（Celery 5.3+ 显式声明，避免弃用告警）
    broker_connection_retry_on_startup=True,
    # 默认：任务结果不持久化（无 result backend，此配置仅作未来接 backend 时的默认值）
    task_ignore_result=True,
    # 预取策略：默认 prefetch 4，重活队列（parser/GPU）在单元③按任务级覆盖
    worker_prefetch_multiplier=4,
    # 时区统一 UTC，与 DB 存储一致
    timezone="UTC",
    enable_utc=True,
    # 任务发现：worker 启动时导入 celery_tasks 注册任务（Celery 在 app finalize 时
    # 才 import，不会与 celery_app 自身构成循环导入）。
    include=("app.modules.ingestion.celery_tasks",),
)

# 别名：`celery -A app.core.celery_app` 默认找名为 `app`/`celery` 的属性，
# 暴露 alias 保证命令行免写 `:celery_app` 也能定位实例。
app = celery_app
