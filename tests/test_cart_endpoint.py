"""GET /cart：整個購物車（全校，不分學系）給頁首購物車抽屜用（獨立測試資料庫；絕不碰開發庫）。

購物車＝有決定冊數 N ≥ 1 的書；沒決定與決定 0（明確排除）都不在裡面。
只列「現在還在採購清單裡」的書（與採購表同一個 purchasable() 條件），所以數字與採購表一致。
"""
import pytest
from fastapi.testclient import TestClient

from library_agent import dashboard
from library_agent.db.models import PurchaseDecision, VerifiedBook
from tests.test_purchase_endpoint import add_rec

A, B, C = "title:a", "title:b", "title:c"


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", isolated_db)
    return TestClient(dashboard.app)


def decide(S, key, copies, suggested_at=1):
    from datetime import datetime, timezone
    with S() as s:
        s.add(PurchaseDecision(book_key=key, copies=copies, suggested_at_decision=suggested_at,
                               decided_at=datetime.now(timezone.utc)))
        s.commit()


def test_empty_cart(client, isolated_db):
    body = client.get("/cart").json()

    assert body == {"ok": True, "items": [], "books": 0, "copies": 0}


def test_only_books_decided_above_zero_are_in_the_cart(client, isolated_db):
    add_rec(isolated_db, A, suggested=5, n=1)
    add_rec(isolated_db, B, suggested=3, n=2)
    add_rec(isolated_db, C, suggested=2, n=3)
    decide(isolated_db, A, 12)
    decide(isolated_db, B, 0)               # 明確排除：不在購物車
                                            # C 沒決定：不在購物車

    body = client.get("/cart").json()

    assert [i["book_key"] for i in body["items"]] == [A]
    assert body["books"] == 1 and body["copies"] == 12


def test_an_item_carries_what_the_drawer_shows(client, isolated_db):
    add_rec(isolated_db, A, suggested=5, n=1)
    decide(isolated_db, A, 12)

    item = client.get("/cart").json()["items"][0]

    assert item == {"book_key": A, "title": "Intro", "copies": 12, "suggested": 5,
                    "priority": "medium", "status": "missing"}


def test_totals_add_up_the_items(client, isolated_db):
    add_rec(isolated_db, A, suggested=5, n=1)
    add_rec(isolated_db, B, suggested=3, n=2)
    decide(isolated_db, A, 12)
    decide(isolated_db, B, 4)

    body = client.get("/cart").json()

    assert body["books"] == 2 and body["copies"] == 16 == sum(i["copies"] for i in body["items"])


def test_a_decision_without_a_current_recommendation_is_not_listed(client, isolated_db):
    decide(isolated_db, "title:gone", 9)        # 建議後來被重建掉了：對不上任何一本書，不列也不算

    body = client.get("/cart").json()

    assert body["items"] == [] and body["books"] == 0 and body["copies"] == 0


def test_a_book_blocked_by_the_pairing_audit_leaves_the_cart(client, isolated_db):
    add_rec(isolated_db, A, suggested=5, n=1)
    decide(isolated_db, A, 12)
    with isolated_db() as s:
        s.query(VerifiedBook).update({"pairing_verdict": "mismatch", "review_status": "pending"})
        s.commit()

    body = client.get("/cart").json()

    assert body["items"] == [] and body["copies"] == 0       # 與採購表一樣：被擋下的書不能買


def test_sorted_by_priority_then_title(client, isolated_db):
    from library_agent.db.models import Recommendation
    add_rec(isolated_db, A, suggested=5, n=1)
    add_rec(isolated_db, B, suggested=3, n=2)
    add_rec(isolated_db, C, suggested=2, n=3)
    with isolated_db() as s:
        s.query(Recommendation).filter(Recommendation.book_key == C).update({"priority": "high"})
        s.commit()
    for k in (A, B, C):
        decide(isolated_db, k, 1)

    keys = [i["book_key"] for i in client.get("/cart").json()["items"]]

    assert keys[0] == C and sorted(keys[1:]) == [A, B]        # 高優先級在前；同優先級依書名再依 book_key


def test_load_data_reports_the_school_wide_cart_totals(client, isolated_db, monkeypatch):
    add_rec(isolated_db, A, suggested=5, n=1)
    decide(isolated_db, A, 12)

    data = dashboard._load_data()

    assert data["cart"] == {"books": 1, "copies": 12}
