"""POST /review/decide 與 /review/undo（獨立測試資料庫；決定一律不碰開發庫）。"""
import pytest
from fastapi.testclient import TestClient

from library_agent import dashboard
from tests.test_review_actions import fields, seed_book, vb_of


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(dashboard, "SessionLocal", isolated_db)
    return TestClient(dashboard.app)


def test_decide_endpoint_applies_to_all_ids_and_reports_skipped(client, isolated_db):
    a, b = seed_book(isolated_db, 1), seed_book(isolated_db, 2)
    taken = seed_book(isolated_db, 3, review="approved")

    body = client.post("/review/decide", json={"ids": [a, b, taken], "action": "none"}).json()

    assert body == {"ok": True, "changed": 2, "skipped": [taken]}
    assert vb_of(isolated_db, a).review_status == "rejected" and vb_of(isolated_db, taken).review_status == "approved"


def test_decide_endpoint_custom_stores_title_and_isbn(client, isolated_db):
    vb = seed_book(isolated_db, 1)

    resp = client.post("/review/decide", json={"ids": [vb], "action": "custom", "title": "我要買的書", "isbn": "0306406152"})

    assert resp.json()["ok"] is True
    assert fields(vb_of(isolated_db, vb)) == ("approved", "custom", "我要買的書", "[]", "9780306406157")


@pytest.mark.parametrize("payload", [
    {"ids": [1], "action": "bogus"},
    {"ids": [1], "action": "custom", "title": "   "},
    {"ids": [1], "action": "custom", "title": "x", "isbn": "123"},
    {"ids": [1], "action": "custom", "title": 123},
    {"ids": "1", "action": "none"},
    {"ids": ["1"], "action": "none"},
    {"ids": [True], "action": "none"},
    {"ids": [], "action": "none"},
    {"ids": list(range(1, 202)), "action": "none"},
    {"action": "none"},
])
def test_decide_endpoint_400_on_bad_input_and_writes_nothing(client, isolated_db, payload):
    vb = seed_book(isolated_db, 1)             # id 1：參數化的 ids 都包含它

    resp = client.post("/review/decide", json=payload)

    assert resp.status_code == 400 and resp.json()["ok"] is False and resp.json()["message"]
    assert fields(vb_of(isolated_db, vb)) == ("pending", None, None, None, None)


def test_decide_endpoint_requires_json(client, isolated_db):
    vb = seed_book(isolated_db, 1)

    resp = client.post("/review/decide", content=f'{{"ids": [{vb}], "action": "none"}}',
                       headers={"Content-Type": "text/plain"})

    assert 400 <= resp.status_code < 500
    assert vb_of(isolated_db, vb).review_status == "pending"          # 一般表單／純文字的跨站請求改不了資料


def test_undo_endpoint_restores_pending(client, isolated_db):
    vb = seed_book(isolated_db, 1)
    client.post("/review/decide", json={"ids": [vb], "action": "original"})

    body = client.post("/review/undo", json={"ids": [vb]}).json()

    assert body == {"ok": True, "changed": 1, "skipped": []}
    assert fields(vb_of(isolated_db, vb)) == ("pending", None, None, None, None)


def test_undo_endpoint_400_on_bad_ids(client):
    assert client.post("/review/undo", json={"ids": []}).status_code == 400
    assert client.post("/review/undo", json={"ids": ["x"]}).status_code == 400
