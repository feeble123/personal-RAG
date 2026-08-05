"""认证模块 Pydantic 模型。"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,50}$")


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=64)
    nickname: str | None = Field(None, max_length=50)

    @field_validator("username")
    @classmethod
    def check_username(cls, v: str) -> str:
        if not USERNAME_RE.match(v):
            raise ValueError("用户名须为 3-50 位字母/数字/下划线")
        return v


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6, max_length=64)


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    nickname: str | None = None
    is_active: bool = True

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
