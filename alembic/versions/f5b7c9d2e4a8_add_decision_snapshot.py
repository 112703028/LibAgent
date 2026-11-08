"""purchase_decisions 記下書名與課綱書目 id（識別鍵改變時對帳搬移用）

Revision ID: f5b7c9d2e4a8
Revises: c4a9e7d1f2b6
Create Date: 2026-11-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f5b7c9d2e4a8'
down_revision: Union[str, Sequence[str], None] = 'c4a9e7d1f2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 用目前的採購建議補上既有決定的快照：書名取代表書目的採購書名（沒有就用驗證器配到的書名），
# 書目 id 取這筆建議的關聯列（去重、遞增）。對不上任何建議的決定維持 NULL（不猜）。
BACKFILL_SQL = """
UPDATE purchase_decisions AS d
SET title = sub.title, citation_ids = sub.citation_ids
FROM (
    SELECT r.book_key,
           COALESCE(vb.purchase_title, vb.canonical_title) AS title,
           COALESCE((SELECT json_agg(x.citation_id ORDER BY x.citation_id)::text
                     FROM (SELECT DISTINCT rc.citation_id
                           FROM recommendation_courses AS rc
                           WHERE rc.recommendation_id = r.id) AS x), '[]') AS citation_ids
    FROM recommendations AS r
    JOIN holding_checks AS hc ON hc.id = r.holding_id
    JOIN verified_books AS vb ON vb.id = hc.verified_book_id
    WHERE r.book_key IS NOT NULL
) AS sub
WHERE d.book_key = sub.book_key
"""


def upgrade() -> None:
    op.add_column("purchase_decisions", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("purchase_decisions", sa.Column("citation_ids", sa.Text(), nullable=True))
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    op.drop_column("purchase_decisions", "citation_ids")
    op.drop_column("purchase_decisions", "title")
