"""三级角色依赖判定单元测试（单元 I 补充）：require_admin / require_superadmin 边界。

- require_admin：superadmin 与 admin 都算管理员，放行；user 拒绝
- require_superadmin：仅 superadmin 放行；admin / user 拒绝
"""
from __future__ import annotations

import pytest

from app.core.deps import require_admin, require_superadmin
from app.core.exceptions import BizError
from app.db.models import User


def _user(role: str) -> User:
    """构造一个未持久化的 User（仅依赖函数读 role，不触 DB/relationship）。"""
    return User(username=f"u_{role}", password_hash="x", role=role)


class TestRequireAdmin:
    async def test_superadmin_allowed(self):
        assert (await require_admin(_user("superadmin"))).role == "superadmin"

    async def test_admin_allowed(self):
        assert (await require_admin(_user("admin"))).role == "admin"

    async def test_user_rejected(self):
        with pytest.raises(BizError) as exc:
            await require_admin(_user("user"))
        assert exc.value.status_code == 403


class TestRequireSuperadmin:
    async def test_superadmin_allowed(self):
        assert (await require_superadmin(_user("superadmin"))).role == "superadmin"

    async def test_admin_rejected(self):
        with pytest.raises(BizError) as exc:
            await require_superadmin(_user("admin"))
        assert exc.value.status_code == 403

    async def test_user_rejected(self):
        with pytest.raises(BizError) as exc:
            await require_superadmin(_user("user"))
        assert exc.value.status_code == 403
