"""审计日志服务（P2-10 / 单元 I）。

- `record_audit()`：把一次管理员敏感操作写入 audit_logs 表（append-only）。
  用独立会话提交，与业务事务解耦——即便业务提交失败，审计也应尽量留痕；
  写失败只记 warning，绝不因审计失败阻断业务。
- 写审计绝不放敏感值进 detail（只存文档名/用户名/ID 等摘要）。
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.models import AuditLog
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


async def record_audit(
    *,
    actor_id: int | None,
    actor_name: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    detail: str = "",
    client_ip: str | None = None,
) -> None:
    """写入一条审计日志（独立会话，失败不影响业务）。"""
    try:
        async with async_session_factory() as db:
            db.add(
                AuditLog(
                    actor_id=actor_id,
                    actor_name=actor_name or "",
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    detail=(detail or "")[:500],
                    client_ip=client_ip,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能阻断业务
        logger.warning("审计日志写入失败 action=%s", action, exc_info=True)


async def list_audit_logs(
    *,
    page: int = 1,
    page_size: int = 20,
    action: str | None = None,
    q: str | None = None,
) -> tuple[list[AuditLog], int]:
    """分页查询审计日志（管理员），支持按 action 过滤 + 按 actor_name/detail 模糊搜索。"""
    from sqlalchemy import func

    where = []
    if action:
        where.append(AuditLog.action == action)
    if q:
        where.append(
            (AuditLog.actor_name.contains(q)) | (AuditLog.detail.contains(q))
        )

    async with async_session_factory() as db:
        total = (
            await db.scalar(select(func.count()).select_from(AuditLog).where(*where))
        ) or 0
        rows = await db.execute(
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows.scalars().all()), total
