"""认证路由：注册 / 登录 / 当前用户 / 修改密码。

注册/登录按 IP 限流（slowapi）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.db.session import get_db
from app.modules.auth.schemas import (
    LoginIn,
    PasswordChangeIn,
    RegisterIn,
    TokenOut,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token(user: User) -> TokenOut:
    return TokenOut(
        access_token=create_access_token(user.id, {"role": user.role}),
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=TokenOut, status_code=201)
@limiter.limit(settings.auth_rate_limit)
async def register(request: Request, body: RegisterIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise BizError("用户名已存在", 409, "USER_EXISTS")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        nickname=body.nickname or body.username,
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _issue_token(user)


@router.post("/login", response_model=TokenOut)
@limiter.limit(settings.auth_rate_limit)
async def login(request: Request, body: LoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    user = await db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise BizError("用户名或密码错误", 401, "INVALID_CREDENTIALS")
    if not user.is_active:
        raise BizError("账号已被禁用", 403, "FORBIDDEN")
    return _issue_token(user)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.put("/password", response_model=UserOut)
async def change_password(
    body: PasswordChangeIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    if not verify_password(body.old_password, user.password_hash):
        raise BizError("原密码错误", 400, "WRONG_OLD_PASSWORD")
    if body.old_password == body.new_password:
        raise BizError("新密码不能与原密码相同", 400, "SAME_PASSWORD")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)
