"""切片记录书内印刷页码（chunks.printed_page）。

问题：引用里显示的「第 171 页」是 PDF 物理页序，而用户手里那本书印的是「157」，
差的是封面/版权/前言/目录那 14 页——能点开原文，却翻不到那一页。
MinerU 本来就逐页识别了印刷页码，只是被当成页眉噪音丢掉了。

本迁移只加列，不回填；历史数据用 `scripts/backfill_printed_page.py` 从
`data/mineru_output/**/ *_content_list.json` 重新算，不用重跑解析。

Revision ID: l3c4d5e6f7a8
Revises: k2b3c4d5e6f7
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l3c4d5e6f7a8"
down_revision: Union[str, None] = "k2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("printed_page", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "printed_page")
