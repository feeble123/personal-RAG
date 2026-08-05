"""限流：slowapi。认证接口按 IP，问答接口按用户 ID（未登录退回 IP）。"""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.security import decode_token


def _rate_key(request: Request) -> str:
    """优先按 JWT 用户维度限流（chat），未登录按 IP。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = decode_token(auth[7:])
            return f"user:{payload.get('sub')}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_key)
