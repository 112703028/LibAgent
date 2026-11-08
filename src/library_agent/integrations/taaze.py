"""TAAZE 讀冊生活：書商後備驗證的第一家（純 HTTP）。

搜尋結果直接寫在 HTML 裡（每筆一個 div.media）；查無結果的頁面也有一個 div.media，但裡面沒有書名，要略過。
TAAZE 的伺服器 TLS 憑證鏈設定錯誤：葉憑證由「Sectigo Public Server Authentication CA EV R36」簽發，
伺服器卻送舊的中繼憑證，所以在 certifi 的根憑證之外額外載入 Sectigo 官方的那張中繼憑證（憑證檢查仍然開著）。
任何錯誤都回空清單：書商查不到不能讓 pipeline 中斷。
"""
import functools
import logging
import re
import ssl
from pathlib import Path

import certifi
import httpx
from bs4 import BeautifulSoup

from library_agent.config import get_settings
from library_agent.integrations.bookstore import Cooldown, Pacer, StoreRecord, split_authors

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.taaze.tw/rwd_searchResult.html"
_CERT = Path(__file__).parent / "certs" / "sectigo_public_server_auth_ca_ev_r36.pem"
_PACER = Pacer(min_interval=2.0)
# 被限流（429）時退讓：照 Retry-After 冷卻，冷卻期間不送任何請求
_COOLDOWN = Cooldown()
_TIMEOUT = 20.0
# 書名結尾的商品標記，例如「(二手書)」：不是書名的一部分
_TAG = re.compile(r"\s*[\(（][^)）]*(?:二手書|電子書|套書|限量|特價|平裝|精裝|預購)[^)）]*[\)）]\s*$")


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(_CERT))
    return ctx


def _client() -> httpx.Client:
    return httpx.Client(verify=_ssl_context(), timeout=_TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": get_settings().crawler_user_agent})


def _text(node, prefix: str = "") -> str:
    if node is None:
        return ""
    text = node.get_text(" ", strip=True)
    return re.sub(rf"^\s*{prefix}\s*[:：]\s*", "", text) if prefix else text


def parse_search_html(html: str) -> list[StoreRecord]:
    records = []
    for block in BeautifulSoup(html, "html.parser").select("div.media"):
        link = block.select_one("h4.media-heading a")
        if link is None:              # 查無結果時頁面上那個不是書目的 div.media
            continue
        title = _TAG.sub("", link.get_text(" ", strip=True)).strip()
        if not title:
            continue
        records.append(StoreRecord(
            title=title,
            authors=split_authors(_text(block.select_one("div.author"))),
            isbn_13=None,             # 搜尋結果沒有 ISBN
            publisher=_text(block.select_one("div.publisher"), "出版社"),
            published=_text(block.select_one("div.pubDate"), "出版日期"),
            source="taaze",
        ))
    return records


def search(query: str) -> list[StoreRecord]:
    if _COOLDOWN.active():
        return []
    try:
        _PACER.wait()
        with _client() as client:
            response = client.get(SEARCH_URL, params={"keyType[]": "0", "keyword[]": query})
            if response.status_code == 429:
                log.warning("TAAZE 回 429（太頻繁），%d 秒內不再查詢", _COOLDOWN.start(response))
                return []
            response.raise_for_status()
            return parse_search_html(response.text)
    except Exception as e:  # noqa: BLE001 — 書商出錯一律當作沒找到
        log.warning("TAAZE 搜尋失敗（%s）：%s", query[:40], e)
        return []
