"""Celery 入库任务：把 DB 真相源的入库 worker 接到 Celery 队列上（单元 J 单元③）。

设计原则（P2-2 原计划 + 单元 J 计划）：
- **task 只接收 job_id**，任务真相仍在 PostgreSQL（IngestionJob 表）。
- 幂等：task 启动时 CAS 领任务（UPDATE..WHERE id+stage='queued'），抢不到（已终态/
  已被人领/不存在）直接返回——重复投递、重试都不会重复处理。
- async 代码用 `asyncio.run()` 包一层（现有 Chroma/embedding/DB 全是 async），
  复用的是 manager 里现成的完整流水线（含心跳续租、协作式取消、失败回写）。

import 策略：manager 会拉起 chromadb/parser 等重依赖，故 **延迟到任务函数体内 import**，
避免 Celery 纯 inspect/其他命令在无重依赖场景 import 失败，也加快 worker 启动。
"""
from __future__ import annotations

import asyncio
import logging

from app.core.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="ingestion.process_job", bind=True, max_retries=0, acks_late=True)
def process_ingestion_job(self, job_id: int) -> None:
    """处理单个入库 job：CAS 领任务 → 执行（幂等）。失败向上抛，交给队列/重试策略。

    acks_late=True（单元④）——worker「真正跑完才回执」，而非「拿到就回执」：
    - worker 在跑完前崩溃 → 任务没回执 → Celery 重新投递给别的 worker，**不丢 job**
    - 重投后 `process_job_from_celery` 再抢 CAS 锁：原 worker 若已跑完（succeeded），
      抢不到直接跳过 → **不重复入库**（幂等）
    - 业务失败（解析/入库错误）已在 `_execute_job` 内回写 DB failed 并吞掉，任务层
      正常返回不回抛 → 不会因业务失败被重投；只有基础设施异常（如 DB 连不上）才
      reject 重投，配合 CAS 幂等保证不重复。
    """
    from app.modules.ingestion import manager  # 延迟 import，避免重依赖拖慢 worker 启动

    try:
        asyncio.run(manager.process_job_from_celery(job_id))
    except Exception:
        # 详细现场已由 manager._execute_job 记录（job 状态会落库 failed），这里只留痕，
        # 重新抛出让 Celery 按任务级重试/ack 策略处理（单元④ 配置 acks_late）。
        logger.exception("Celery 入库任务异常 job=%s", job_id)
        raise
