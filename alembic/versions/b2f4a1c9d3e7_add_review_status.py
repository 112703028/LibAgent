"""add review_status to verified_books

Revision ID: b2f4a1c9d3e7
Revises: 9a9afb132aea
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2f4a1c9d3e7'
down_revision: Union[str, Sequence[str], None] = '9a9afb132aea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """人工審核決定的狀態：NULL / pending / approved / rejected。"""
    op.add_column(
        'verified_books',
        sa.Column('review_status', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('verified_books', 'review_status')
