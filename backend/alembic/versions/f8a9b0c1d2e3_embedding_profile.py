"""P1-3 embedding profile：EmbeddingCache 加 profile_fingerprint + 复合主键

- 新增 profile_fingerprint 列（默认 ""，兼容旧缓存）
- 主键 content_hash → (content_hash, profile_fingerprint)：同一内容不同 embedding 配置分开缓存

SQLite 改主键需重建表（batch 模式：新建 → 拷贝 → 删旧 → 改名）。
Revision ID: f8a9b0c1d2e3
Revises: f7a8b9c0d1e2
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('embedding_cache', schema=None) as batch_op:
        batch_op.add_column(sa.Column('profile_fingerprint', sa.String(length=32), nullable=False, server_default=sa.text("''")))
        # SQLite：重建表以改复合主键（batch 自动处理）
        batch_op.alter_column(
            'content_hash',
            existing_type=sa.String(length=64),
            nullable=False,
            new_column_name='content_hash',
        )

    # SQLite batch 改主键：drop 旧主键 + 建复合主键（batch 模式支持）
    with op.batch_alter_table('embedding_cache', schema=None) as batch_op:
        batch_op.create_primary_key('pk_embedding_cache', ['content_hash', 'profile_fingerprint'])


def downgrade() -> None:
    with op.batch_alter_table('embedding_cache', schema=None) as batch_op:
        batch_op.create_primary_key('pk_embedding_cache', ['content_hash'])
        batch_op.drop_column('profile_fingerprint')
