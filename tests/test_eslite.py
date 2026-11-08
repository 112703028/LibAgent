"""誠品搜尋（不連網、不開瀏覽器：解析存下來的真實回應，瀏覽器工作階段用假的取代）。"""
import json
import time
from pathlib import Path

import pytest

from library_agent.integrations import eslite
from library_agent.integrations.bookstore import Pacer

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(eslite, "_PACER", Pacer(min_interval=0))
    eslite.close()                   # 每個測試從乾淨狀態開始（close 也會解除停用）
    yield
    eslite.close()


def data():
    return json.loads((FIX / "eslite_search.json").read_text(encoding="utf-8"))


class FakeSession:
    def __init__(self, answer=None):
        self.answer = answer if answer is not None else data()
        self.params = []
        self.closed = False

    def fetch_json(self, params):
        self.params.append(params)
        return self.answer

    def close(self):
        self.closed = True


def test_parse_keeps_books_only_and_converts_isbn10():
    records = eslite.parse_search_json(data())

    assert [r.isbn_13 for r in records] == ["9789861371955", "9789861372273"]      # is_book "no" 的被排除
    assert records[0].publisher == "究竟出版社股份有限公司" and records[0].published == "2014-10-30"
    assert records[0].source == "eslite"


def test_parse_splits_slash_joined_authors():
    assert eslite.parse_search_json(data())[0].authors == ["岸見一郎", "古賀史健"]


def test_parse_tolerates_missing_fields():
    assert eslite.parse_search_json({}) == []
    (r,) = eslite.parse_search_json({"results": [{"name": "某書", "is_book": "yes", "isbn10": "123"}]})
    assert r.title == "某書" and r.isbn_13 is None and r.authors == [] and r.publisher == ""   # 檢查碼不過＝沒有 ISBN


def test_search_uses_the_session_and_passes_the_query_as_a_parameter(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(eslite, "_launch_browser", lambda: session)
    query = 'C++ & Java #1 "引號" 100%'

    records = eslite.search(query)

    assert len(records) == 2
    assert session.params == [{"q": query, "page_size": "20", "page_no": "1"}]     # 原字串交給頁面裡的 URLSearchParams


def test_the_browser_is_launched_once_and_reused(monkeypatch):
    launched = []
    monkeypatch.setattr(eslite, "_launch_browser", lambda: launched.append(1) or FakeSession())

    eslite.search("甲")
    eslite.search("乙")

    assert launched == [1]


def test_search_returns_empty_and_disables_when_launch_fails(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("找不到 Edge")

    monkeypatch.setattr(eslite, "_launch_browser", boom)

    assert eslite.search("被討厭的勇氣") == []
    assert eslite.search("另一本") == []
    assert calls == [1]                                    # 失敗一次就在這次執行內停用，不再每本書都等


def test_search_times_out_without_hanging(monkeypatch):
    def slow():
        time.sleep(3)
        return FakeSession()

    monkeypatch.setattr(eslite, "_launch_browser", slow)

    started = time.monotonic()
    assert eslite.search("被討厭的勇氣", timeout=0.5) == []
    assert time.monotonic() - started < 1.5
    assert eslite.search("另一本") == []                   # 逾時後停用


def test_close_releases_the_browser_and_is_idempotent(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(eslite, "_launch_browser", lambda: session)
    eslite.search("甲")

    eslite.close()
    eslite.close()

    assert session.closed


def test_close_lifts_the_disable_for_the_next_run(monkeypatch):
    monkeypatch.setattr(eslite, "_launch_browser", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    eslite.search("甲")
    eslite.close()                                         # validator 結束
    monkeypatch.setattr(eslite, "_launch_browser", lambda: FakeSession())

    assert len(eslite.search("乙")) == 2                   # 下一次執行會再試一次誠品
