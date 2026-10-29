"""add progress columns to pipeline_node_runs

Revision ID: f4a7b3d1e9c2
Revises: e3f6a2c9d8b1
Create Date: 2026-10-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4a7b3d1e9c2'
down_revision: Union[str, Sequence[str], None] = 'e3f6a2c9d8b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """節點處理進度（dashboard 進度區顯示「已處理 X / 共 Y 本」與最新訊息用）。

    四欄皆 nullable：既有的歷史列、以及沒有迴圈的節點都不會有進度。
    """
    op.add_column('pipeline_node_runs', sa.Column('processed', sa.Integer(), nullable=True))
    op.add_column('pipeline_node_runs', sa.Column('total', sa.Integer(), nullable=True))
    op.add_column('pipeline_node_runs', sa.Column('unit', sa.String(10), nullable=True))
    op.add_column('pipeline_node_runs', sa.Column('message', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('pipeline_node_runs', 'message')
    op.drop_column('pipeline_node_runs', 'unit')
    op.drop_column('pipeline_node_runs', 'total')
    op.drop_column('pipeline_node_runs', 'processed')
