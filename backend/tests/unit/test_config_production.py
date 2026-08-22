"""P0-1 单元1：APP_ENV 生产模式 fail-safe 校验。

目标：`APP_ENV=production` 时，缺安全必需配置 → 构造 Settings 即抛错，启动直接失败。
       development/test 不受影响，未配置 secret/密码时自动 fallback，保证本地可跑。

测试用 `_env_file=None` + monkeypatch 清掉 conftest 注入的环境变量，隔离验证。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉可能从 conftest/.env 泄漏进校验的环境变量，保证只测本用例。"""
    for key in ("APP_ENV", "JWT_SECRET", "ADMIN_PASSWORD", "EMBEDDING_API_KEY",
                "DEEPSEEK_API_KEY", "DEBUG"):
        monkeypatch.delenv(key, raising=False)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    _clean(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


class TestProductionFails:
    def test_production_missing_jwt_secret(self, monkeypatch):
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            _settings(monkeypatch, APP_ENV="production", ADMIN_PASSWORD="Str0ngPass!",
                      EMBEDDING_API_KEY="sf-x", DEEPSEEK_API_KEY="sk-x")

    def test_production_default_admin_password(self, monkeypatch):
        # 未设 ADMIN_PASSWORD（回落到默认 123456）→ 拒绝
        with pytest.raises(ValidationError, match="ADMIN_PASSWORD"):
            _settings(monkeypatch, APP_ENV="production", JWT_SECRET="x" * 40,
                      EMBEDDING_API_KEY="sf-x", DEEPSEEK_API_KEY="sk-x")

    def test_production_explicit_123456_password(self, monkeypatch):
        # 显式写死 123456 同样拒绝（默认弱口令禁止上线）
        with pytest.raises(ValidationError, match="ADMIN_PASSWORD"):
            _settings(monkeypatch, APP_ENV="production", JWT_SECRET="x" * 40,
                      ADMIN_PASSWORD="123456", EMBEDDING_API_KEY="sf-x",
                      DEEPSEEK_API_KEY="sk-x")

    def test_production_missing_embedding_key(self, monkeypatch):
        with pytest.raises(ValidationError, match="EMBEDDING_API_KEY"):
            _settings(monkeypatch, APP_ENV="production", JWT_SECRET="x" * 40,
                      ADMIN_PASSWORD="Str0ngPass!", DEEPSEEK_API_KEY="sk-x")

    def test_production_missing_deepseek_key(self, monkeypatch):
        with pytest.raises(ValidationError, match="DEEPSEEK_API_KEY"):
            _settings(monkeypatch, APP_ENV="production", JWT_SECRET="x" * 40,
                      ADMIN_PASSWORD="Str0ngPass!", EMBEDDING_API_KEY="sf-x")

    def test_production_debug_true_rejected(self, monkeypatch):
        with pytest.raises(ValidationError, match="DEBUG"):
            _settings(monkeypatch, APP_ENV="production", JWT_SECRET="x" * 40,
                      ADMIN_PASSWORD="Str0ngPass!", EMBEDDING_API_KEY="sf-x",
                      DEEPSEEK_API_KEY="sk-x", DEBUG="true")

    def test_production_all_configured_ok(self, monkeypatch):
        s = _settings(monkeypatch, APP_ENV="production", JWT_SECRET="x" * 40,
                      ADMIN_PASSWORD="Str0ngPass!", EMBEDDING_API_KEY="sf-x",
                      DEEPSEEK_API_KEY="sk-x")
        assert s.app_env == "production"
        assert s.jwt_secret == "x" * 40

    def test_invalid_app_env_rejected(self, monkeypatch):
        with pytest.raises(ValidationError, match="APP_ENV"):
            _settings(monkeypatch, APP_ENV="staging", JWT_SECRET="x" * 40)


class TestDevAndTestUnaffected:
    def test_development_default_gets_random_secret(self, monkeypatch):
        s = _settings(monkeypatch)  # 无任何 env，默认 development
        assert s.app_env == "development"
        assert s.jwt_secret, "development 未配置 secret 应自动生成"
        assert len(s.jwt_secret) >= 32
        assert s.admin_password == "123456"

    def test_development_custom_values_respected(self, monkeypatch):
        s = _settings(monkeypatch, JWT_SECRET="my-dev-secret", ADMIN_PASSWORD="devpass")
        assert s.jwt_secret == "my-dev-secret"
        assert s.admin_password == "devpass"

    def test_test_env_with_minimal_keys(self, monkeypatch):
        s = _settings(monkeypatch, APP_ENV="test", JWT_SECRET="t" * 40,
                      ADMIN_PASSWORD="pass123", EMBEDDING_API_KEY="sf-x",
                      DEEPSEEK_API_KEY="sk-x")
        assert s.app_env == "test"

    def test_dev_missing_model_keys_ok(self, monkeypatch):
        # development 不强制模型 key（本地可用 fake embedding/LLM）
        s = _settings(monkeypatch, APP_ENV="development")
        assert s.embedding_api_key == ""
        assert s.deepseek_api_key == ""

    def test_app_env_normalized_lowercase(self, monkeypatch):
        s = _settings(monkeypatch, APP_ENV="Development")
        assert s.app_env == "development"
