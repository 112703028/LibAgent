"""add department to courses

Revision ID: c1a2b3d4e5f6
Revises: 00516dcee674
Create Date: 2026-09-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a2b3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '00516dcee674'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """開課系級（xlsx「開課系級 Department and Level」欄），供 dashboard 依系所篩選。"""
    op.add_column('courses', sa.Column('department', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('courses', 'department')
