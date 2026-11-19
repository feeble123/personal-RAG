"""记录切片在原 PDF 上的位置框（chunks.bbox）。

为什么需要：扫描件（如《水文预报》第 5 版）**没有文本层**，PyMuPDF 搜索不到任何字，
光靠 `chunk.page` 只能把用户送到那一页，却指不出是哪一段/哪张表。
MinerU 解析时其实算出了每个块的坐标（`middle.json` 里是 PDF 点坐标），
只是当初没存下来。

本迁移只加列，不回填；历史数据用 `scripts/backfill_chunk_bbox.py` 从
`data/mineru_output/**/*_middle.json` 对齐后回填，**不需要重灌**。

存的是 PDF 点坐标（origin 左上、y 向下），与 PyMuPDF 的 `page.rect` 同一坐标系：
`{"rects": [[x0,y0,x1,y1], ...], "source": "mineru", "page_idx": 170}`

Revision ID: m4d5e6f7a8b9
Revises: l3c4d5e6f7a8
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m4d5e6f7a8b9"
down_revision: Union[str, None] = "l3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("bbox", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "bbox")
