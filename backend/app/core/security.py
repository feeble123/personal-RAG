"""安全工具：密码哈希（bcrypt）+ JWT 签发/校验 + refresh token 会话。

- access token：短期 JWT，携带 session_version（sv），改密/禁用后旧 token 立即失效
- refresh token：不透明随机串，服务端只存 sha256 哈希，轮换吊销（防重放）
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings


# ---------- 密码 ----------
def hash_password(password: str) -> str:
    """bcrypt 哈希（自动加盐）。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------- JWT ----------
def create_access_token(
    subject: str | int,
    extra: dict | None = None,
    *,
    session_version: int = 0,
    expires_delta: timedelta | None = None,
) -> str:
    """签发短期 access token。`sv`=session_version：改密/禁用后旧 token 失效。"""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload: dict = {"sub": str(subject), "exp": expire, "sv": session_version}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码 JWT，失败抛 jwt.PyJWTError。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---------- refresh token 会话 ----------
REFRESH_COOKIE = "refresh_token"
REFRESH_TOKEN_BYTES = 48  # 384 位随机熵


def generate_refresh_token() -> str:
    """生成不透明随机 refresh token（返回明文，调用方只存哈希）。"""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """refresh token 的 sha256 哈希（落库值，绝不明文存）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

