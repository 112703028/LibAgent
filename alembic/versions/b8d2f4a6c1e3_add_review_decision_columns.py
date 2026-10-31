"""add review decision and purchase-title columns to verified_books

Revision ID: b8d2f4a6c1e3
Revises: a7c4e1b9d3f2
Create Date: 2026-10-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8d2f4a6c1e3'
down_revision: Union[str, Sequence[str], None] = 'a7c4e1b9d3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """儀表板審核按鈕的決定。全部可為 NULL，現有資料不動。

    purchase_* 是「採購書名」（買原書／使用者輸入時填），不覆蓋 canonical_*（驗證器配到的證據）；
    review_applied_at 為 NULL 代表決定還沒反映到館藏與採購建議（「套用審核結果」的待辦標記）。
    """
    op.add_column('verified_books', sa.Column('review_choice', sa.String(10), nullable=True))
    op.add_column('verified_books', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('verified_books', sa.Column('review_applied_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('verified_books', sa.Column('purchase_title', sa.String(500), nullable=True))
    op.add_column('verified_books', sa.Column('purchase_authors', sa.Text(), nullable=True))
    op.add_column('verified_books', sa.Column('purchase_isbn', sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column('verified_books', 'purchase_isbn')
    op.drop_column('verified_books', 'purchase_authors')
    op.drop_column('verified_books', 'purchase_title')
    op.drop_column('verified_books', 'review_applied_at')
    op.drop_column('verified_books', 'reviewed_at')
    op.drop_column('verified_books', 'review_choice')
