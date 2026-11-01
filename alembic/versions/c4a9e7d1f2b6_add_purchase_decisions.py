"""add purchase_decisions

Revision ID: c4a9e7d1f2b6
Revises: b8d2f4a6c1e3
Create Date: 2026-10-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4a9e7d1f2b6'
down_revision: Union[str, Sequence[str], None] = 'b8d2f4a6c1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """人對採購冊數的決定。以 book_key 為鍵、獨立於 recommendations：
    recommendations 是衍生資料（librarian／recommender 會刪掉重建），決定不能跟著消失。"""
    op.create_table(
        'purchase_decisions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('book_key', sa.String(500), nullable=False),
        sa.Column('copies', sa.Integer(), nullable=False),
        sa.Column('suggested_at_decision', sa.Integer(), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('book_key', name='uq_purchase_decisions_book_key'),
    )


def downgrade() -> None:
    op.drop_table('purchase_decisions')
