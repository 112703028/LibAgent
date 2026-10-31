"""recommender 讀「採購書名」：書名與同書認定都用 buy_*（人工決定「買原書」的書不再與配到的書合併）。"""
from sqlalchemy import select

from library_agent.agents import recommender as R
from library_agent.db.models import Recommendation, VerifiedBook
from tests.test_recommender_node import ISBN_A, book, node, seed   # noqa: F401  （node 是 fixture）


def test_recommendation_uses_the_purchase_title_and_isbn_for_title_and_book_key(node):
    # 兩門課的書目都配到同一個 ISBN；其中一個被人工決定「買原書」（沒有有效 ISBN）→ 不再與另一個合併
    (_c1, _vb1, _h1), (_c2, vb2, _h2) = seed(node.Session, book("C1", title="Intro"), book("C2", title="Intro"))
    with node.Session() as s:
        row = s.get(VerifiedBook, vb2)
        row.review_status, row.review_choice = "approved", "original"
        row.purchase_title, row.purchase_authors, row.purchase_isbn = "Intro 原書", "[]", None
        s.commit()

    R.recommender_node({})

    with node.Session() as s:
        keys = sorted(s.scalars(select(Recommendation.book_key)))
    assert len(keys) == 2 and f"isbn:{ISBN_A}" in keys                 # 另一筆用採購書名認定，不再是同一本
    assert sorted(call[0] for call in node.llm_calls) == ["Intro", "Intro 原書"]
