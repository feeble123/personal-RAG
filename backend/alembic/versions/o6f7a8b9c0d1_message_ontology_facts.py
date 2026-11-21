"""助手消息保存本轮的本体事实（刷新会话后卡片还在）。

以前本体事实**只走 SSE**：`facts` 事件发给前端，前端塞进当前这条流式消息里，
既没落库、也没进历史接口。于是刷新页面、或者过一会儿再打开这个会话，
卡片就没了——只剩一段文字答案，「这到底是本体 RAG 还是普通 RAG」当场看不出来。

存整个 facts 事件体（facts / sources / notes / latency_ms），而不是只存 facts 数组：
前端 `OntologyFactCards` 读的就是这个形状，存原样以后加字段不用再改表。

Revision ID: o6f7a8b9c0d1
Revises: n5e6f7a8b9c0
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "o6f7a8b9c0d1"
down_revision: Union[str, None] = "n5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _json():
    return postgresql.JSONB(astext_type=sa.Text()) if _is_pg() else sa.JSON()


def upgrade() -> None:
    op.add_column("messages", sa.Column("ontology_facts", _json(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "ontology_facts")
