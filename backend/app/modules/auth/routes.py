"""认证路由：注册 / 登录 / 当前用户 / 修改密码 / refresh 轮换 / 退出。

P0-1 认证加固：
- access token 短期（15min），携带 session_version（sv）——改密/禁用后旧 token 立即失效
- refresh token 不透明随机串，只存 sha256 哈希于 auth_sessions 表
- /auth/refresh 轮换：旧 session 吊销 + 发新（重放检测）
- 登录/注册成功后 refresh 放 HttpOnly cookie，浏览器自动携带

注册/登录按 IP 限流（slowapi）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.exceptions import BizError
from app.core.ratelimit import limiter
from app.core.security import (
    REFRESH_COOKIE,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.models import AuthSession, User
from app.db.session import get_db
from app.modules.auth.schemas import (
    LoginIn,
    PasswordChangeIn,
    RegisterIn,
    TokenOut,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _now() -> datetime:
    """SQLite DateTime 存 naive（无时区），统一用 naive UTC 避免 aware/naive 比较错误。"""
    return datetime.utcnow()


def _set_refresh_cookie(resp: Response, token: str) -> None:
    """refresh token 只放 HttpOnly cookie（JS 读不到，XSS 无法窃取）。"""
    resp.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        path="/api/auth",
    )


def _clear_refresh_cookie(resp: Response) -> None:
    resp.delete_cookie(key=settings.refresh_cookie_name, path="/api/auth")


async def _issue_token(
    db: AsyncSession, user: User, resp: Response
) -> TokenOut:
    """签发 access + 写 session + 种 refresh cookie，一次完成。"""
    refresh = generate_refresh_token()
    session = AuthSession(
        user_id=user.id,
        refresh_hash=hash_refresh_token(refresh),
        expires_at=_now() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(session)
    await db.flush()  # 先落库 session，随后 commit 由调用方统一提交
    _set_refresh_cookie(resp, refresh)
    return TokenOut(
        access_token=create_access_token(user.id, {"role": user.role}, session_version=user.session_version),
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=TokenOut, status_code=201)
@limiter.limit(settings.auth_rate_limit)
async def register(
    request: Request, body: RegisterIn, resp: Response, db: AsyncSession = Depends(get_db)
) -> TokenOut:
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
    out = await _issue_token(db, user, resp)
    await db.commit()
    return out


@router.post("/login", response_model=TokenOut)
@limiter.limit(settings.auth_rate_limit)
async def login(
    request: Request, body: LoginIn, resp: Response, db: AsyncSession = Depends(get_db)
) -> TokenOut:
    user = await db.scalar(select(User).where(User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise BizError("用户名或密码错误", 401, "INVALID_CREDENTIALS")
    if not user.is_active:
        raise BizError("账号已被禁用", 403, "FORBIDDEN")
    out = await _issue_token(db, user, resp)
    await db.commit()
    return out


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/refresh", response_model=TokenOut)
@limiter.limit(settings.refresh_rate_limit)
async def refresh(request: Request, resp: Response, db: AsyncSession = Depends(get_db)) -> TokenOut:
    """refresh 轮换：校验 cookie → 吊销旧 session → 签发新 session + 新 access。

    重放检测：同一个 refresh token 第二次使用 → 其哈希已被轮换吊销 → 拒绝。
    """
    old_refresh = request.cookies.get(settings.refresh_cookie_name)
    if not old_refresh:
        raise BizError("未提供刷新令牌", 401, "UNAUTHORIZED")

    old_hash = hash_refresh_token(old_refresh)
    session = await db.scalar(select(AuthSession).where(AuthSession.refresh_hash == old_hash))
    if not session:
        raise BizError("刷新令牌无效", 401, "UNAUTHORIZED")
    if session.revoked_at is not None:
        # 已吊销（可能是重放）：拒绝。为防更激进攻击，这里只拒绝当前请求。
        raise BizError("刷新令牌已被吊销", 401, "UNAUTHORIZED")
    now = _now()
    if session.expires_at < now:
        raise BizError("登录会话已过期，请重新登录", 401, "UNAUTHORIZED")

    user = await db.get(User, session.user_id)
    if not user:
        raise BizError("用户不存在", 401, "UNAUTHORIZED")
    if not user.is_active:
        raise BizError("账号已被禁用", 403, "FORBIDDEN")

    # 轮换：吊销旧 session，生成新 session + 新 refresh cookie
    session.revoked_at = now
    session.last_used_at = now
    out = await _issue_token(db, user, resp)
    await db.commit()
    return out


@router.post("/logout", status_code=204)
async def logout(request: Request, resp: Response, db: AsyncSession = Depends(get_db)) -> Response:
    """吊销当前 refresh session + 清 cookie。"""
    refresh = request.cookies.get(settings.refresh_cookie_name)
    if refresh:
        rhash = hash_refresh_token(refresh)
        await db.execute(
            update(AuthSession)
            .where(AuthSession.refresh_hash == rhash, AuthSession.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        await db.commit()
    _clear_refresh_cookie(resp)
    resp.status_code = 204
    return resp


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
    # 改密：session_version +1 → 所有旧 access token 失效 + 吊销全部 refresh session
    user.session_version = (user.session_version or 0) + 1
    now = _now()
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)
