"""購物車決定的快照：決定時記下這本書的書名與它由哪些課綱書目組成（獨立測試資料庫）。

書的識別鍵（book_key）之後可能改變（書商補上 ISBN、審核改書名、重跑 pipeline）；
課綱書目的 id 不會因為重新驗證而改變，所以是「這是哪本書」的線索，供重算後對帳搬移。
"""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from library_agent import dashboard
from library_agent.db.models import (
    Citation, Course, PurchaseDecision, Recommendation, RecommendationCourse,
)
from tests.test_purchase_endpoint import add_rec

A, B = "title:a", "title:b"
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "f5b7c9d2e4a8_add_decision_snapshot.py"


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", isolated_db)
    return TestClient(dashboard.app)


def link_course(S, key, n):
    """在第 n 門新課加一筆課綱書目，連到 key 這本書的採購建議。回傳新書目的 id。"""
    with S() as s:
        rec = s.scalar(select(Recommendation).where(Recommendation.book_key == key))
        s.add(Course(course_id=f"L{n}", course_name=f"課-L{n}", semester="114-1", source_file="t", raw_content="t"))
        s.flush()
        c = Citation(course_id=f"L{n}", title="Intro", authors="[]", is_required=True, raw_mention="x", confidence=0.9)
        s.add(c)
        s.flush()
        s.add(RecommendationCourse(recommendation_id=rec.id, course_id=f"L{n}", citation_id=c.id, is_required=True,
                                   enrolled=10))
        s.commit()
        return c.id


def book(S, key, n, *, courses=2):
    """一本書：add_rec 建好建議（它沒有關聯列），再連上 courses 筆課綱書目。回傳書目 id（遞增）。"""
    add_rec(S, key, suggested=5, n=n)
    return sorted(link_course(S, key, n * 10 + i) for i in range(courses))


def decision(S, key):
    with S() as s:
        return s.scalar(select(PurchaseDecision).where(PurchaseDecision.book_key == key))


def test_decide_stores_the_title_and_the_citation_ids(client, isolated_db):
    cits = book(isolated_db, A, 1)

    client.post("/purchase/decide", json={"book_key": A, "copies": 3})

    row = decision(isolated_db, A)
    assert row.title == "Intro" and json.loads(row.citation_ids) == cits


def test_decide_many_stores_snapshots_for_each_book(client, isolated_db):
    cits_a, cits_b = book(isolated_db, A, 1), book(isolated_db, B, 2, courses=1)

    client.post("/purchase/decide-many", json={"book_keys": [A, B], "mode": "suggested"})

    assert json.loads(decision(isolated_db, A).citation_ids) == cits_a
    assert json.loads(decision(isolated_db, B).citation_ids) == cits_b
    assert decision(isolated_db, B).title == "Intro"


def test_redeciding_refreshes_the_snapshot(client, isolated_db):
    cits = book(isolated_db, A, 1, courses=1)
    client.post("/purchase/decide", json={"book_key": A, "copies": 3})
    extra = link_course(isolated_db, A, 99)               # 之後這本書多了一門課

    client.post("/purchase/decide", json={"book_key": A, "copies": 4})

    assert json.loads(decision(isolated_db, A).citation_ids) == sorted(cits + [extra])


def test_zero_decisions_are_snapshotted_too(client, isolated_db):
    cits = book(isolated_db, A, 1, courses=1)

    client.post("/purchase/decide-many", json={"book_keys": [A], "mode": "zero"})

    assert json.loads(decision(isolated_db, A).citation_ids) == cits      # 「這次不買」也是決定，鍵變了也要跟著搬


def test_clearing_deletes_the_row_and_needs_no_snapshot(client, isolated_db):
    book(isolated_db, A, 1)
    client.post("/purchase/decide", json={"book_key": A, "copies": 3})

    client.post("/purchase/decide", json={"book_key": A, "copies": None})

    assert decision(isolated_db, A) is None


def _backfill_sql() -> str:
    spec = importlib.util.spec_from_file_location("decision_snapshot_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BACKFILL_SQL


def test_backfill_sql_fills_existing_decisions_and_leaves_unmatched_null(isolated_db):
    from datetime import datetime, timezone
    cits = book(isolated_db, A, 1)
    with isolated_db() as s:                               # 升級前就存在的決定：沒有快照
        now = datetime.now(timezone.utc)
        s.add(PurchaseDecision(book_key=A, copies=3, suggested_at_decision=5, decided_at=now))
        s.add(PurchaseDecision(book_key="title:gone", copies=2, suggested_at_decision=1, decided_at=now))
        s.commit()

    with isolated_db() as s:
        s.execute(text(_backfill_sql()))
        s.commit()

    row = decision(isolated_db, A)
    assert row.title == "Intro" and json.loads(row.citation_ids) == cits
    gone = decision(isolated_db, "title:gone")
    assert gone.title is None and gone.citation_ids is None             # 對不上任何建議：不猜
