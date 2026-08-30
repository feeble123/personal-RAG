"""日志密钥脱敏（P2-10 / 单元 I）。

把 API key、密码、数据库口令、JWT、refresh token 等敏感值从日志里抹掉，
防止「异常堆栈带出 httpx 请求头」「错误信息打印 key」等把密钥写进日志。

两种手段叠加：
1. 通用模式（正则）：Bearer token、sk- 开头密钥、api_key=/password= 赋值、DB URL 口令、JWT。
2. 已知 secret 值（从 settings 读）：字面量替换，兜底「直接 log settings.xxx_key」的写法。

设计原则：脱敏失败绝不抛异常、绝不影响日志输出（宁可漏脱，不可丢日志）。
"""
from __future__ import annotations

import re

REDACT_PLACEHOLDER = "***"

# 通用敏感模式：顺序执行，先长后短、先具体后泛化。
_PATTERNS: list[re.Pattern[str]] = [
    # JWT（eyJ 开头三段 base64url）——先于 Bearer 匹配整串
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Authorization / Bearer 头（含 rerank/embedding/llm 的 httpx 异常堆栈）
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;'\"\\]+"),
    re.compile(r"(?i)(\bbearer\s+)[^\s,;'\"\\]+"),
    # api key / x-api-key / api_key 赋值
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\s,;'\"\\]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;'\"\\]+"),
    # 密码赋值（password= / password:）
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;'\"\\]+"),
    # 硅基流动 / OpenAI 风格密钥 sk-xxxx（脱敏为 sk-***）
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{6,})"),
    # 数据库 URL 里的口令：postgresql://user:PASS@host → user:***@host
    re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"),
]


def _known_secret_values() -> list[str]:
    """从 settings 读当前已知 secret 值（空值/太短不参与，避免误伤普通文本）。"""
    try:
        from app.core.config import settings
    except Exception:  # pragma: no cover - 极端情况（settings 未就绪）跳过
        return []

    values = [
        settings.jwt_secret,
        settings.admin_password,
        settings.embedding_api_key,
        settings.deepseek_api_key,
    ]
    return [v for v in values if v and len(v) >= 6]


def redact(text: str | None) -> str:
    """对一段日志文本做脱敏，返回新字符串（原文本不变，遵循不可变性）。"""
    if not text:
        return text or ""
    result = text
    # 1) 通用模式
    for pattern in _PATTERNS:
        result = pattern.sub(_repl, result)
    # 2) 已知 secret 值字面量替换（兜底）
    for secret in _known_secret_values():
        if secret in result:
            result = result.replace(secret, REDACT_PLACEHOLDER)
    return result


def _repl(match: re.Match[str]) -> str:
    """保留前缀（如 Bearer / password=）只抹掉值，日志仍可读。"""
    groups = match.groups()
    if len(groups) == 2 and groups[0] and groups[1]:
        # 形如 (://user:) + (@)：保留前后，只抹密码
        return f"{groups[0]}{REDACT_PLACEHOLDER}{groups[1]}"
    if groups and groups[0]:
        # 形如 (Bearer ) + 值：保留前缀 + ***
        return f"{groups[0]}{REDACT_PLACEHOLDER}"
    # 无分组（如纯 JWT / sk-xxx）：整串抹掉
    return REDACT_PLACEHOLDER
