"""POST /purchase/decide：記錄人對一本書採購冊數的決定（獨立測試資料庫；絕不碰開發庫）。

copies 為整數 0–9999：0＝這次不買（書仍留在清單），null／省略＝清除決定（採用系統建議）。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from library_agent import dashboard
from library_agent.agents import recommender as R
from library_agent.db.models import (
    Citation, Course, HoldingCheck, PurchaseDecision, Recommendation, VerifiedBook,
)
from tests.test_recommender_node import book, node, seed   # noqa: F401  （node 是 fixture）

KEY = "title:intro"


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", isolated_db)
    return TestClient(dashboard.app)


def add_rec(S, key=KEY, suggested=5, n=1):
    with S() as s:
        s.add(Course(course_id=f"D{n}", course_name=f"課-D{n}", semester="114-1", source_file="t", raw_content="t"))
        s.flush()
        c = Citation(course_id=f"D{n}", title="Intro", authors="[]", is_required=True, raw_mention="x", confidence=0.9)
        s.add(c)
        s.flush()
        vb = VerifiedBook(citation_id=c.id, canonical_title="Intro", canonical_authors="[]", source="nla", verified=True)
        s.add(vb)
        s.flush()
        hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
        s.add(hc)
        s.flush()
        s.add(Recommendation(course_id=f"D{n}", holding_id=hc.id, priority="medium", suggested_copies=suggested,
                             rationale="r", book_key=key))
        s.commit()


def rows(S):
    with S() as s:
        return [(r.book_key, r.copies, r.suggested_at_decision) for r in s.scalars(select(PurchaseDecision))]


def post(client, **payload):
    return client.post("/purchase/decide", json=payload)


# ---------- 寫入 ----------

def test_decide_stores_the_copies_and_the_suggestion_at_that_moment(client, isolated_db):
    add_rec(isolated_db, suggested=5)

    body = post(client, book_key=KEY, copies=12).json()

    assert body == {"ok": True, "copies": 12, "suggested": 5, "final": 12}
    assert rows(isolated_db) == [(KEY, 12, 5)]


def test_deciding_again_updates_instead_of_duplicating(client, isolated_db):
    add_rec(isolated_db, suggested=5)
    post(client, book_key=KEY, copies=12)
    with isolated_db() as s:
        s.query(Recommendation).update({"suggested_copies": 3})        # 系統建議後來變了
        s.commit()

    post(client, book_key=KEY, copies=7)

    assert rows(isolated_db) == [(KEY, 7, 3)]                          # 同一列、冊數與「決定當時的建議」都更新


def test_zero_is_stored_as_zero_and_is_not_a_clear(client, isolated_db):
    add_rec(isolated_db, suggested=5)

    body = post(client, book_key=KEY, copies=0).json()

    assert body["copies"] == 0 and body["final"] == 0
    assert rows(isolated_db) == [(KEY, 0, 5)]


# ---------- 清除 ----------

@pytest.mark.parametrize("payload", [{"copies": None}, {}])
def test_clearing_removes_the_decision_and_final_returns_to_the_suggestion(client, isolated_db, payload):
    add_rec(isolated_db, suggested=5)
    post(client, book_key=KEY, copies=12)

    body = post(client, book_key=KEY, **payload).json()

    assert body == {"ok": True, "copies": None, "suggested": 5, "final": 5}
    assert rows(isolated_db) == []


def test_clearing_a_decision_whose_recommendation_is_gone_still_succeeds(client, isolated_db):
    from datetime import datetime, timezone
    with isolated_db() as s:
        s.add(PurchaseDecision(book_key="orphan", copies=3, suggested_at_decision=1, decided_at=datetime.now(timezone.utc)))
        s.commit()

    body = post(client, book_key="orphan", copies=None).json()

    assert body["ok"] is True and body["suggested"] is None
    assert rows(isolated_db) == []


# ---------- 驗證 ----------

@pytest.mark.parametrize("copies", [-1, 10000, 1.5, "3", True, [], {}])
def test_bad_copies_are_rejected_without_writing(client, isolated_db, copies):
    add_rec(isolated_db)

    resp = post(client, book_key=KEY, copies=copies)

    assert resp.status_code == 400 and resp.json()["ok"] is False and resp.json()["message"]
    assert rows(isolated_db) == []


@pytest.mark.parametrize("key", [None, "", "   ", 5, ["x"]])
def test_missing_or_blank_book_key_is_rejected(client, isolated_db, key):
    add_rec(isolated_db)

    assert post(client, book_key=key, copies=3).status_code == 400
    assert rows(isolated_db) == []


def test_missing_book_key_field_is_rejected(client):
    assert client.post("/purchase/decide", json={"copies": 3}).status_code == 400


def test_unknown_book_key_is_404_and_writes_nothing(client, isolated_db):
    add_rec(isolated_db)

    resp = post(client, book_key="title:nope", copies=3)

    assert resp.status_code == 404 and resp.json()["ok"] is False
    assert rows(isolated_db) == []


def test_decide_requires_json(client, isolated_db):
    add_rec(isolated_db)

    resp = client.post("/purchase/decide", content='{"book_key": "title:intro", "copies": 3}',
                       headers={"Content-Type": "text/plain"})

    assert 400 <= resp.status_code < 500
    assert rows(isolated_db) == []


# ---------- 重算不會洗掉決定 ----------

def test_a_decision_survives_a_recommender_rerun(node, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", node.Session)
    client = TestClient(dashboard.app)
    seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommend()
    (key,) = [k for (k,) in _all_keys(node.Session)]
    assert post(client, book_key=key, copies=12).json()["ok"] is True

    with node.Session() as s:                      # 模擬 librarian／validator 刪掉建議後重建
        s.query(Recommendation).delete()
        s.commit()
    R.recommend()

    assert rows(node.Session) == [(key, 12, 2)]    # 決定還在、仍對得上同一個 book_key
    (rec,) = dashboard._load_data()["recs"]
    assert (rec.book_key, rec.decided, rec.final) == (key, 12, 12)


def _all_keys(S):
    with S() as s:
        return s.execute(select(Recommendation.book_key)).all()
