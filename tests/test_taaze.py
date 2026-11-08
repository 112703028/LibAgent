"""TAAZE 搜尋（不連網：解析存下來的真實回應，請求用 httpx.MockTransport 攔截）。"""
from pathlib import Path

import httpx
import pytest

from library_agent.integrations import taaze
from library_agent.integrations.bookstore import Pacer

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(taaze, "_PACER", Pacer(min_interval=0))
    monkeypatch.setattr(taaze, "_cool_until", 0.0)


def test_parse_hit_page():
    records = taaze.parse_search_html((FIX / "taaze_search_hit.html").read_text(encoding="utf-8"))

    assert len(records) == 3
    first = records[0]
    assert first.title == "被討厭的勇氣：自我啟發之父「阿德勒」的教導"                # 去掉「(二手書)」
    assert first.authors == ["岸見一郎", "古賀史健"]
    assert first.publisher == "究竟出版" and first.published == "2014-10-30"
    assert first.source == "taaze" and first.isbn_13 is None


def test_parse_empty_page_returns_empty_list():
    assert taaze.parse_search_html((FIX / "taaze_search_empty.html").read_text(encoding="utf-8")) == []


def fake_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_search_encodes_special_characters(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=(FIX / "taaze_search_empty.html").read_text(encoding="utf-8"))

    monkeypatch.setattr(taaze, "_client", fake_client(handler))
    query = 'C++ & Java #1 "引號" 100%'

    taaze.search(query)

    (url,) = seen
    assert url.params["keyword[]"] == query and url.params["keyType[]"] == "0"     # 不截斷、不變成另一個參數
    assert str(url).startswith(taaze.SEARCH_URL)


def test_search_returns_records_from_the_page(monkeypatch):
    hit = (FIX / "taaze_search_hit.html").read_text(encoding="utf-8")
    monkeypatch.setattr(taaze, "_client", fake_client(lambda request: httpx.Response(200, text=hit)))

    assert [r.title for r in taaze.search("被討厭的勇氣")][:1] == ["被討厭的勇氣：自我啟發之父「阿德勒」的教導"]


def test_search_returns_empty_list_on_network_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(taaze, "_client", fake_client(handler))

    assert taaze.search("被討厭的勇氣") == []


def test_search_returns_empty_list_on_http_error(monkeypatch):
    monkeypatch.setattr(taaze, "_client", fake_client(lambda request: httpx.Response(503, text="busy")))

    assert taaze.search("被討厭的勇氣") == []


def test_ssl_context_loads_the_extra_intermediate():
    subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in taaze._ssl_context().get_ca_certs()]

    assert "Sectigo Public Server Authentication CA EV R36" in subjects


# ---------- 被限流時退讓（試跑時 TAAZE 回過 429）----------

def counting_client(status, headers=None):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers or {}, text="slow down")

    return calls, fake_client(handler)


def test_a_429_makes_us_back_off_instead_of_hammering_on(monkeypatch):
    calls, client = counting_client(429)
    monkeypatch.setattr(taaze, "_client", client)
    now = [1000.0]
    monkeypatch.setattr(taaze, "_now", lambda: now[0])

    assert taaze.search("甲") == []
    assert taaze.search("乙") == [] and taaze.search("丙") == []

    assert len(calls) == 1                                    # 冷卻期間不再送出任何請求


def test_requests_resume_after_the_cooldown(monkeypatch):
    calls, client = counting_client(429)
    monkeypatch.setattr(taaze, "_client", client)
    now = [1000.0]
    monkeypatch.setattr(taaze, "_now", lambda: now[0])
    taaze.search("甲")

    now[0] += taaze.DEFAULT_COOLDOWN + 1
    taaze.search("乙")

    assert len(calls) == 2


def test_retry_after_is_honored_but_capped(monkeypatch):
    calls, client = counting_client(429, {"Retry-After": "99999"})
    monkeypatch.setattr(taaze, "_client", client)
    now = [1000.0]
    monkeypatch.setattr(taaze, "_now", lambda: now[0])
    taaze.search("甲")

    now[0] += taaze.MAX_COOLDOWN + 1                          # 網站要求等很久，我們最多等 MAX_COOLDOWN
    taaze.search("乙")

    assert len(calls) == 2


def test_other_errors_do_not_start_a_cooldown(monkeypatch):
    calls, client = counting_client(500)
    monkeypatch.setattr(taaze, "_client", client)

    taaze.search("甲")
    taaze.search("乙")

    assert len(calls) == 2
