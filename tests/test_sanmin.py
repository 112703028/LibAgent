"""三民書局搜尋（不連網：解析存下來的真實回應，請求用 httpx.MockTransport 攔截）。

三民是模糊比對、按相關性排序：搜不存在的書名也會回字面上有點像的不相關書，所以「是不是同一本」
一定要靠 match_auditor 的判斷，這裡只負責把搜尋結果整理好。
"""
from pathlib import Path

import httpx
import pytest

from library_agent.integrations import sanmin
from library_agent.integrations.bookstore import Pacer

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(sanmin, "_PACER", Pacer(min_interval=0))
    monkeypatch.setattr(sanmin._COOLDOWN, "until", 0.0)


def page(name):
    return (FIX / name).read_text(encoding="utf-8")


def fake_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_hit_page():
    records = sanmin.parse_search_html(page("sanmin_search_hit.html"))

    assert len(records) == 3
    first = records[0]
    assert first.title == "被討厭的勇氣：自我啟發之父「阿德勒」的教導"
    assert first.authors == ["岸見一郎", "古賀史健"]
    assert first.publisher == "究竟" and first.published == "2014/11/01"
    assert first.source == "sanmin" and first.isbn_13 is None


def test_parse_keeps_the_page_order_which_is_relevance_order():
    titles = [r.title for r in sanmin.parse_search_html(page("sanmin_search_hit.html"))]

    assert titles[0].startswith("被討厭的勇氣：自我啟發") and "二部曲" in titles[1]


def test_parse_unrelated_results_are_still_returned_for_the_judge_to_reject():
    records = sanmin.parse_search_html(page("sanmin_search_unrelated.html"))

    assert [r.title for r in records][:2] == ["不存在的戀人【用不存在的愛，治癒存在的孤獨】", "不存在的詩"]


def test_parse_page_without_results_returns_empty_list():
    assert sanmin.parse_search_html("<html><body><div class='ProductView'></div></body></html>") == []


def test_parse_skips_a_card_without_a_title():
    html = "<div class='Prod'><div class='Info'><div class='Title'></div></div></div>"

    assert sanmin.parse_search_html(html) == []


def test_search_uses_the_keyword_category_and_encodes_special_characters(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, text=page("sanmin_search_unrelated.html"))

    monkeypatch.setattr(sanmin, "_client", fake_client(handler))
    query = 'C++ & Java #1 "引號" 100%'

    sanmin.search(query)

    (url,) = seen
    assert url.params["qu"] == query and url.params["ct"] == "k"                     # 關鍵字搜尋；不截斷、不變成另一個參數
    assert str(url).startswith(sanmin.SEARCH_URL)


def test_search_returns_at_most_the_first_twenty_results(monkeypatch):
    cards = "".join(f"<div class='Prod'><div class='Info'><div class='Title'><a href='/product/index/{i}'><h3>書{i}</h3></a>"
                    "</div></div></div>" for i in range(30))
    monkeypatch.setattr(sanmin, "_client", fake_client(lambda request: httpx.Response(200, text=cards)))

    records = sanmin.search("書")

    assert len(records) == 20 and records[0].title == "書0"                          # 相關性排序，後面的不必看


def test_search_returns_empty_list_on_network_and_http_errors(monkeypatch):
    def down(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sanmin, "_client", fake_client(down))
    assert sanmin.search("甲") == []
    monkeypatch.setattr(sanmin, "_client", fake_client(lambda request: httpx.Response(503, text="busy")))
    assert sanmin.search("甲") == []


def test_a_429_makes_us_back_off(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="slow down")

    monkeypatch.setattr(sanmin, "_client", fake_client(handler))

    assert sanmin.search("甲") == [] and sanmin.search("乙") == []

    assert len(calls) == 1                                                            # 冷卻期間不再送請求


def test_requests_identify_the_project_honestly(monkeypatch):
    sent = {}

    def handler(request):
        sent["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text="")

    monkeypatch.setattr(sanmin, "_client", lambda: sanmin.httpx.Client(
        transport=httpx.MockTransport(handler), headers={"User-Agent": sanmin.get_settings().crawler_user_agent}))

    sanmin.search("甲")

    assert sent["ua"] == sanmin.get_settings().crawler_user_agent and "Mozilla" not in sent["ua"]
