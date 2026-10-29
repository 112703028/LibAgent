"""add pairing audit columns to verified_books

Revision ID: e3f6a2c9d8b1
Revises: d2e5f8a1b4c7
Create Date: 2026-10-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f6a2c9d8b1'
down_revision: Union[str, Sequence[str], None] = 'd2e5f8a1b4c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """match_auditor 稽核「配到的權威記錄是否真的是原書」的結果。

    pairing_verdict 同時當「已稽核」標記：NULL 表示還沒稽核，重跑時才會處理，
    避免對同一筆重複呼叫 LLM。
    """
    op.add_column('verified_books', sa.Column('pairing_verdict', sa.String(10), nullable=True))
    op.add_column('verified_books', sa.Column('pairing_judge', sa.String(10), nullable=True))
    op.add_column('verified_books', sa.Column('pairing_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('verified_books', 'pairing_reason')
    op.drop_column('verified_books', 'pairing_judge')
    op.drop_column('verified_books', 'pairing_verdict')
