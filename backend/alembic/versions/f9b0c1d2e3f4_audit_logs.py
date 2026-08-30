"""P2-10 单元I：audit_logs 审计日志表（append-only 管理员操作留痕）

- 只增不删不改：无 UPDATE/DELETE 接口，无 onupdate 时间戳
- actor_id SET NULL（操作人账号被删时保留日志），actor_name 冗余快照
Revision ID: f9b0c1d2e3f4
Revises: f9a0b1c2d3e4
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9b0c1d2e3f4'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # 幂等：开发/调试时 create_all 可能已建表（无 alembic 版本记录），存在则跳过。
    if not insp.has_table('audit_logs'):
        op.create_table(
            'audit_logs',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('actor_id', sa.Integer(), nullable=True),
            sa.Column('actor_name', sa.String(length=50), nullable=False),
            sa.Column('action', sa.String(length=50), nullable=False),
            sa.Column('target_type', sa.String(length=30), nullable=False),
            sa.Column('target_id', sa.String(length=50), nullable=True),
            sa.Column('detail', sa.String(length=500), nullable=False),
            sa.Column('client_ip', sa.String(length=50), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
            sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_audit_logs_actor_id', 'audit_logs', ['actor_id'], unique=False)
        op.create_index('ix_audit_logs_action', 'audit_logs', ['action'], unique=False)
        op.create_index('ix_audit_logs_created', 'audit_logs', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_logs_created', table_name='audit_logs')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
    op.drop_index('ix_audit_logs_actor_id', table_name='audit_logs')
    op.drop_table('audit_logs')
