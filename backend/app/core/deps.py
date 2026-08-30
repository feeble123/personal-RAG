"""FastAPI 依赖注入：数据库会话 / 当前用户 / 管理员权限。"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BizError
from app.core.security import decode_token
from app.db.models import User
from app.db.session import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]


def _extract_token(authorization: str | None) -> str:
    if not authorization:
        raise BizError("未登录或登录已过期", 401, "UNAUTHORIZED")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise BizError("无效的认证格式", 401, "UNAUTHORIZED")
    return parts[1]


async def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """解析 JWT 并加载当前用户。

    P0-1：校验 token 携带的 sv（session_version）== 用户当前版本。
    改密/禁用/重置密码会使 session_version +1 → 旧 token 的 sv 落后 → 401。
    """
    token = _extract_token(authorization)
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub"))
        token_sv = int(payload.get("sv") or 0)
    except Exception:
        raise BizError("登录已过期，请重新登录", 401, "UNAUTHORIZED")

    user = await db.get(User, user_id)
    if not user:
        raise BizError("用户不存在", 401, "UNAUTHORIZED")
    if not user.is_active:
        raise BizError("账号已被禁用", 403, "FORBIDDEN")
    if (user.session_version or 0) != token_sv:
        raise BizError("登录已过期，请重新登录", 401, "UNAUTHORIZED")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# 三级角色（单元 I 补充）：superadmin 超管 / admin 库管 / user 普通。
# 库管与超管都算「管理员」，可进知识库/记忆库/审计/统计等管理接口。
ADMIN_ROLES = ("admin", "superadmin")


async def require_admin(user: CurrentUser) -> User:
    """管理接口（知识库/记忆库/审计/统计）：库管(admin)与超管(superadmin)都可访问。"""
    if user.role not in ADMIN_ROLES:
        raise BizError("无权限：仅管理员可访问", 403, "FORBIDDEN")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def require_superadmin(user: CurrentUser) -> User:
    """账号管理接口：仅超管(superadmin)可访问——管「人」的最高权限，库管无权。"""
    if user.role != "superadmin":
        raise BizError("无权限：仅超级管理员可访问", 403, "FORBIDDEN")
    return user


SuperAdminUser = Annotated[User, Depends(require_superadmin)]


async def get_client_ip(request: Request) -> str:
    """取客户端 IP（用于限流按 IP 维度）。"""
    if request.client:
        return request.client.host
    return "unknown"
