"""POST /purchase/decide-many：批次加入購物車／設為不買／清除決定（獨立測試資料庫）。

mode：
  suggested  決定冊數＝伺服器端「當下」的系統建議（＝加入購物車）；系統建議為 0 的略過
  zero       決定冊數＝0（這次不買，明確排除）
  clear      刪除決定（回到還沒決定、不在購物車）
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from library_agent import dashboard
from library_agent.agents import recommender as R
from library_agent.db.models import PurchaseDecision, Recommendation
from tests.test_purchase_endpoint import add_rec, rows
from tests.test_recommender_node import book, node, seed   # noqa: F401  （node 是 fixture）

A, B, C = "title:a", "title:b", "title:c"


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", isolated_db)
    return TestClient(dashboard.app)


def many(client, keys, mode):
    return client.post("/purchase/decide-many", json={"book_keys": keys, "mode": mode})


def world(S):
    add_rec(S, A, suggested=5, n=1)
    add_rec(S, B, suggested=3, n=2)
    add_rec(S, C, suggested=0, n=3)           # 系統建議 0（已有足夠館藏）


# ---------- suggested ----------

def test_suggested_sets_each_book_to_its_current_suggestion(client, isolated_db):
    world(isolated_db)

    body = many(client, [A, B], "suggested").json()

    assert body["ok"] is True and body["changed"] == 2 and body["skipped"] == []
    assert sorted(rows(isolated_db)) == [(A, 5, 5), (B, 3, 3)]
    assert body["results"] == {A: {"copies": 5, "final": 5}, B: {"copies": 3, "final": 3}}


def test_suggested_uses_the_servers_current_value_not_a_stale_one(client, isolated_db):
    world(isolated_db)
    many(client, [A], "suggested")
    with isolated_db() as s:
        s.query(Recommendation).filter(Recommendation.book_key == A).update({"suggested_copies": 9})
        s.commit()

    many(client, [A], "suggested")

    assert rows(isolated_db) == [(A, 9, 9)]                       # 再加入一次＝採用現在的建議


def test_suggested_skips_books_whose_suggestion_is_zero_and_reports_them(client, isolated_db):
    world(isolated_db)

    body = many(client, [A, C], "suggested").json()

    assert body["changed"] == 1 and body["skipped"] == [C]
    assert rows(isolated_db) == [(A, 5, 5)]                       # 沒有東西可加的書不會變成 0 冊的決定


# ---------- zero ----------

def test_zero_marks_books_as_explicitly_excluded(client, isolated_db):
    world(isolated_db)

    body = many(client, [A, C], "zero").json()

    assert body["changed"] == 2 and body["skipped"] == []
    assert sorted(rows(isolated_db)) == [(A, 0, 5), (C, 0, 0)]
    assert body["results"][A] == {"copies": 0, "final": 0}


# ---------- clear ----------

def test_clear_removes_decisions_even_when_the_recommendation_is_gone(client, isolated_db):
    world(isolated_db)
    many(client, [A, B], "suggested")
    with isolated_db() as s:
        s.add(PurchaseDecision(book_key="orphan", copies=3, suggested_at_decision=1, decided_at=datetime.now(timezone.utc)))
        s.commit()

    body = many(client, [A, "orphan", "never-decided"], "clear").json()

    assert body["ok"] is True and body["changed"] == 2 and body["skipped"] == []     # 沒有決定的 key 不算略過
    assert sorted(rows(isolated_db)) == [(B, 3, 3)]
    assert body["results"][A] == {"copies": None, "final": 0}


# ---------- 找不到 ----------

def test_unknown_keys_are_skipped_not_fatal(client, isolated_db):
    world(isolated_db)

    body = many(client, [A, "title:nope"], "suggested").json()

    assert body["ok"] is True and body["changed"] == 1 and body["skipped"] == ["title:nope"]
    assert rows(isolated_db) == [(A, 5, 5)]


def test_duplicate_keys_in_one_request_are_handled_once(client, isolated_db):
    world(isolated_db)

    body = many(client, [A, A, A], "suggested").json()

    assert body["changed"] == 1 and rows(isolated_db) == [(A, 5, 5)]


# ---------- 驗證 ----------

@pytest.mark.parametrize("payload", [
    {"mode": "zero"},                                              # 沒有 book_keys
    {"book_keys": [], "mode": "zero"},
    {"book_keys": [f"k{i}" for i in range(201)], "mode": "zero"},  # 超過 200
    {"book_keys": "title:a", "mode": "zero"},                      # 不是清單
    {"book_keys": ["title:a", ""], "mode": "zero"},                # 含空白
    {"book_keys": ["title:a", "   "], "mode": "zero"},
    {"book_keys": ["title:a", 5], "mode": "zero"},                 # 含非字串
    {"book_keys": ["title:a"], "mode": "bogus"},                   # 未知 mode
    {"book_keys": ["title:a"]},                                    # 沒有 mode
])
def test_bad_requests_are_rejected_without_writing(client, isolated_db, payload):
    world(isolated_db)

    resp = client.post("/purchase/decide-many", json=payload)

    assert resp.status_code == 400 and resp.json()["ok"] is False and resp.json()["message"]
    assert rows(isolated_db) == []


def test_a_bad_item_means_nothing_at_all_is_written(client, isolated_db):
    world(isolated_db)

    resp = client.post("/purchase/decide-many", json={"book_keys": [A, B, 7], "mode": "suggested"})

    assert resp.status_code == 400 and rows(isolated_db) == []      # 有效的 A、B 也沒有寫入


def test_decide_many_requires_json(client, isolated_db):
    world(isolated_db)

    resp = client.post("/purchase/decide-many", content='{"book_keys": ["title:a"], "mode": "zero"}',
                       headers={"Content-Type": "text/plain"})

    assert 400 <= resp.status_code < 500 and rows(isolated_db) == []


# ---------- 重算不會洗掉 ----------

def test_a_batch_decision_survives_a_recommender_rerun(node, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", node.Session)
    client = TestClient(dashboard.app)
    seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommend()
    (key,) = [k for (k,) in _keys(node.Session)]
    assert many(client, [key], "suggested").json()["changed"] == 1

    with node.Session() as s:                       # 模擬 librarian／validator 刪掉建議後重建
        s.query(Recommendation).delete()
        s.commit()
    R.recommend()

    assert [r[0] for r in rows(node.Session)] == [key]


def _keys(S):
    with S() as s:
        return s.execute(select(Recommendation.book_key)).all()
