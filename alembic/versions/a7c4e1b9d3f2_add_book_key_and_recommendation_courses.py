"""add book_key to recommendations and the recommendation_courses table

Revision ID: a7c4e1b9d3f2
Revises: e3f6a2c9d8b1
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c4e1b9d3f2'
down_revision: Union[str, Sequence[str], None] = 'e3f6a2c9d8b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """採購建議從「一門課 × 一本書一筆」改成「一本書一筆」。

    只加不改、向下相容：book_key 可為 NULL，舊程式（尚未改寫的 recommender）在新結構上仍能運作，
    舊格式的列會在新版 recommender 下次執行時被清掉並重建（recommendations 完全是推導資料）。
    """
    op.add_column('recommendations', sa.Column('book_key', sa.String(500), nullable=True))
    # 唯一索引允許多個 NULL（Postgres 把 NULL 視為彼此不同），所以重建前舊列不會互相衝突
    op.create_index('uq_recommendations_book_key', 'recommendations', ['book_key'], unique=True)

    # 兩個外鍵都要 ON DELETE CASCADE：
    #   recommendation_id —— validator.py / librarian.py 會直接 DELETE recommendations
    #   citation_id       —— parser.py 重寫某門課時會先刪舊 citations
    op.create_table(
        'recommendation_courses',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('recommendation_id', sa.Integer(),
                  sa.ForeignKey('recommendations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('course_id', sa.String(20), sa.ForeignKey('courses.course_id'), nullable=False),
        sa.Column('citation_id', sa.Integer(),
                  sa.ForeignKey('citations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False),
        sa.Column('enrolled', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('recommendation_id', 'citation_id', name='uq_reccourse_rec_citation'),
    )


def downgrade() -> None:
    op.drop_table('recommendation_courses')
    op.drop_index('uq_recommendations_book_key', table_name='recommendations')
    op.drop_column('recommendations', 'book_key')
