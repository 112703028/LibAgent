"""三民書局：書商後備驗證的另一家（純 HTTP，不需要瀏覽器、沒有被擋）。

搜尋是 GET /search/?ct=k&qu=關鍵字（ct=k 是關鍵字搜尋），結果直接寫在 HTML 裡：每筆一個 div.Prod，
書名在 div.Title h3、作者在 span.Author a、出版社在 span.Publisher a、出版日在 span.PublishDate（沒有 ISBN）。
三民是**模糊比對、按相關性排序**：搜不存在的書名也會回字面上有點像的不相關書，所以是不是同一本一定要靠
match_auditor 的判斷；這裡只取前 20 筆（相關性高的在前面）。
請求用專案自己的 User-Agent，不偽裝成瀏覽器。任何錯誤都回空清單；被限流（429）時依 Retry-After 退讓。
"""
import logging
import re

import httpx
from bs4 import BeautifulSoup

from library_agent.config import get_settings
from library_agent.integrations.bookstore import Cooldown, Pacer, StoreRecord

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.sanmin.com.tw/search/"
MAX_RESULTS = 20
_PACER = Pacer(min_interval=2.0)
_COOLDOWN = Cooldown()
_TIMEOUT = 20.0


def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": get_settings().crawler_user_agent})


def _field(card, selector: str, label: str = "") -> str:
    """欄位文字；去掉開頭的「作者：」「出版日：」這類標籤。"""
    node = card.select_one(selector)
    if node is None:
        return ""
    text = node.get_text(" ", strip=True)
    return re.sub(rf"^{label}\s*[:：]\s*", "", text) if label else text


def parse_search_html(html: str) -> list[StoreRecord]:
    records = []
    for card in BeautifulSoup(html, "html.parser").select("div.Prod"):
        title_node = card.select_one("div.Title h3")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title:
            continue
        authors = [a.get_text(strip=True) for a in card.select("span.Author a") if a.get_text(strip=True)]
        records.append(StoreRecord(
            title=title,
            authors=authors,
            isbn_13=None,             # 搜尋結果沒有 ISBN
            publisher=_field(card, "span.Publisher a"),
            published=_field(card, "span.PublishDate", "出版日"),
            source="sanmin",
        ))
    return records


def search(query: str) -> list[StoreRecord]:
    if _COOLDOWN.active():
        return []
    try:
        _PACER.wait()
        with _client() as client:
            response = client.get(SEARCH_URL, params={"ct": "k", "qu": query})
            if response.status_code == 429:
                log.warning("三民回 429（太頻繁），%d 秒內不再查詢", _COOLDOWN.start(response))
                return []
            response.raise_for_status()
            return parse_search_html(response.text)[:MAX_RESULTS]
    except Exception as e:  # noqa: BLE001 — 書商出錯一律當作沒找到
        log.warning("三民搜尋失敗（%s）：%s", query[:40], e)
        return []
