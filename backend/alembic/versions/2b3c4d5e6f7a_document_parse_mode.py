"""单元 S：documents 表加 parse_mode（上传时用户自选快速/高精度解析）

- 新增 parse_mode 列：fast=快速（pipeline 老后端）/ high=高精度（hybrid-engine 新后端）
- 旧数据回填 fast（既有文档都是 pipeline 老后端解析的）

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b3c4d5e6f7a'
down_revision: Union[str, None] = '1a2b3c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'documents',
        sa.Column('parse_mode', sa.String(length=10), nullable=False, server_default='fast'),
    )


def downgrade() -> None:
    op.drop_column('documents', 'parse_mode')
