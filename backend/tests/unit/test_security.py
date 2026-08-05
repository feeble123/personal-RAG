"""安全模块单元测试：密码哈希 / JWT 签发校验。"""
from __future__ import annotations

import jwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token, decode_token, hash_password, verify_password


class TestPassword:
    def test_hash_and_verify_roundtrip(self):
        hashed = hash_password("my-secret-password")
        assert hashed != "my-secret-password"  # 必须加盐哈希，不存明文
        assert verify_password("my-secret-password", hashed) is True

    def test_wrong_password_rejected(self):
        hashed = hash_password("correct-password")
        assert verify_password("wrong-password", hashed) is False

    def test_same_password_different_salt(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # bcrypt 随机盐，两次哈希不同
        assert verify_password("same", h1) and verify_password("same", h2)

    def test_verify_garbage_hash(self):
        assert verify_password("x", "not-a-valid-hash") is False


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token(42, {"role": "admin"})
        payload = decode_token(token)
        assert payload["sub"] == "42"
        assert payload["role"] == "admin"

    def test_wrong_secret_rejected(self):
        token = create_access_token(1)
        wrong = "x" * 40  # 足够长的错误密钥（避免 InsecureKeyLengthWarning）
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, wrong, algorithms=[settings.jwt_algorithm])

    def test_tampered_token_rejected(self):
        token = create_access_token(1)
        tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
        with pytest.raises(jwt.PyJWTError):
            decode_token(tampered)

    def test_expired_token_rejected(self):
        from datetime import datetime, timedelta, timezone

        exp = datetime.now(timezone.utc) - timedelta(minutes=1)
        expired = jwt.encode(
            {"sub": "1", "exp": exp}, settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(expired)
