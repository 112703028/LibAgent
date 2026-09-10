"""add superseded_by_iteration to citations

Revision ID: d2e5f8a1b4c7
Revises: c1a2b3d4e5f6
Create Date: 2026-09-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2e5f8a1b4c7'
down_revision: Union[str, Sequence[str], None] = 'c1a2b3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """AI 推薦書被回饋迴路換掉時，標記第幾輪被取代（保留舊列供追溯）。"""
    op.add_column('citations', sa.Column('superseded_by_iteration', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('citations', 'superseded_by_iteration')
