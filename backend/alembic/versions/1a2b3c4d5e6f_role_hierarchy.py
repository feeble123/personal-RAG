"""单元 I 补充：三级角色分层（superadmin 超管 / admin 库管 / user 普通）。

数据迁移：存量 role='admin' 的老管理员全部升级为 'superadmin'（无感升级）。
- 新角色 'admin' 语义变为「库管」——可管知识库/记忆库/审计，但不能管账号。
- 幂等：重复执行安全（SET 同值无副作用）。
Revision ID: 1a2b3c4d5e6f
Revises: f9b0c1d2e3f4
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = 'f9b0c1d2e3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 存量 admin → superadmin（无感升级，老管理员不丢权）。
    # 幂等：升级后无 role='admin' 行，再次执行影响 0 行，安全。
    op.execute("UPDATE users SET role='superadmin' WHERE role='admin'")


def downgrade() -> None:
    # 回退：把全部超管降回 admin（无法区分「本就是超管」与「升级来的」，故全量回退）。
    op.execute("UPDATE users SET role='admin' WHERE role='superadmin'")
