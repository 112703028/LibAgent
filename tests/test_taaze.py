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
