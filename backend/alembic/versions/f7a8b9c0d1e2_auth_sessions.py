"""P0-1 认证加固：users.session_version + auth_sessions 表

- users 加 session_version 列（默认 0）：改密/禁用/重置密码时 +1 → 旧 access token 全部失效
- 新增 auth_sessions 表：refresh token 的 sha256 哈希（绝不明文）+ 轮换吊销标记

Revision ID: f7a8b9c0d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-08-22
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # users.session_version（改密/禁用时 +1 使旧 token 失效）
    if 'session_version' not in [c['name'] for c in insp.get_columns('users')]:
        op.add_column(
            'users',
            sa.Column('session_version', sa.Integer(), server_default=sa.text('0'), nullable=False),
        )

    # auth_sessions 表（refresh token 哈希 + 吊销标记）
    # 幂等：开发/调试时 create_all 可能已建表（无 alembic 版本记录），存在则跳过。
    # 注意：refresh_hash 用 UniqueConstraint（与 ORM unique=True 一致），
    # 不要用 create_index(unique=True) —— SQLite 下 compare_metadata 会判为与 ORM 不一致。
    if not insp.has_table('auth_sessions'):
        op.create_table(
            'auth_sessions',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('refresh_hash', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('refresh_hash'),
        )
        op.create_index('ix_auth_sessions_user_id', 'auth_sessions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_auth_sessions_user_id', table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_column('users', 'session_version')
