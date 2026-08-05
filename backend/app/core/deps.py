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
    """解析 JWT 并加载当前用户。"""
    token = _extract_token(authorization)
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub"))
    except Exception:
        raise BizError("登录已过期，请重新登录", 401, "UNAUTHORIZED")

    user = await db.get(User, user_id)
    if not user:
        raise BizError("用户不存在", 401, "UNAUTHORIZED")
    if not user.is_active:
        raise BizError("账号已被禁用", 403, "FORBIDDEN")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> User:
    """仅管理员可访问的知识库管理接口。"""
    if user.role != "admin":
        raise BizError("无权限：仅管理员可访问", 403, "FORBIDDEN")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def get_client_ip(request: Request) -> str:
    """取客户端 IP（用于限流按 IP 维度）。"""
    if request.client:
        return request.client.host
    return "unknown"
