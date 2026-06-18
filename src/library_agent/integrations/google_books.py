import re
import time
from dataclasses import dataclass

import httpx

from library_agent.config import get_settings

_BASE_URL = "https://www.googleapis.com/books/v1/volumes"
_TIMEOUT = 10.0
_MAX_RETRIES = 5


@dataclass
class BookRecord:
    canonical_title: str
    canonical_authors: list[str]
    isbn_13: str | None
    isbn_10: str | None
    source: str = "google_books"


def _extract(item: dict) -> BookRecord:
    info = item.get("volumeInfo", {})
    isbn_13 = isbn_10 = None
    for identifier in info.get("industryIdentifiers", []):
        if identifier["type"] == "ISBN_13":
            isbn_13 = identifier["identifier"]
        elif identifier["type"] == "ISBN_10":
            isbn_10 = identifier["identifier"]
    return BookRecord(
        canonical_title=info.get("title", ""),
        canonical_authors=info.get("authors", []),
        isbn_13=isbn_13,
        isbn_10=isbn_10,
    )


def _search(query: str, api_key: str | None) -> BookRecord | None:
    params: dict = {"q": query, "maxResults": 1, "fields": "items/volumeInfo"}
    if api_key:
        params["key"] = api_key

    backoff = 2.0
    for attempt in range(_MAX_RETRIES):
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_BASE_URL, params=params)

        if response.status_code == 429:
            wait = backoff * (2 ** attempt)
            time.sleep(wait)
            continue

        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            return None
        return _extract(items[0])

    # 重試耗盡仍 429 → 拋例外，讓 validator 標成 ERROR 而非 MISS
    raise RuntimeError(f"Google Books rate limited after {_MAX_RETRIES} retries")


def _clean_title(title: str) -> str:
    # Remove edition markers: "5th Edition", "14th ed.", "2nd Ed"
    title = re.sub(r",?\s*\d+\w*\s+[Ee]d(?:ition)?\.?.*", "", title)
    title = re.sub(r"\s*\(.*?\)", "", title)
    return title.strip()


def _strip_subtitle(title: str) -> str:
    # "World Politics: Trend & Transformation" → "World Politics"
    return re.sub(r"\s*:.*", "", title).strip()


def lookup(
    title: str,
    authors: list[str] | None = None,
    isbn: str | None = None,
) -> BookRecord | None:
    api_key = get_settings().google_books_api_key
    first_author = authors[0].split(",")[0].strip() if authors else None

    # 1. ISBN 查詢（最準確）
    if isbn:
        result = _search(f"isbn:{isbn}", api_key)
        if result:
            return result

    # 2. 書名 + 作者查詢
    if first_author:
        result = _search(f'intitle:"{title}" inauthor:"{first_author}"', api_key)
        if result:
            return result

    # 3. 只用書名查詢
    result = _search(f'intitle:"{title}"', api_key)
    if result:
        return result

    # 4. 清理版次後再查（處理 "Management, 14th ed." 這類標題）
    clean = _clean_title(title)
    if clean != title and clean:
        if first_author:
            result = _search(f'intitle:"{clean}" inauthor:"{first_author}"', api_key)
            if result:
                return result
        result = _search(f'intitle:"{clean}"', api_key)
        if result:
            return result

    # 5. 去掉副標後再查（只在有作者時才做，避免 "Management" 誤中其他書）
    # 處理 "World Politics: Trend & Transformation" 這類冒號在 query 裡被 Google 解析成 operator 的情況
    if first_author:
        short = _strip_subtitle(clean if (clean != title and clean) else title)
        if short not in {title, clean} and short:
            result = _search(f'intitle:"{short}" inauthor:"{first_author}"', api_key)
            if result:
                return result

    return None

if __name__ == "__main__":
    # 測試用例
    record = lookup(
        title="Microeconomics: Theory and Applications with Calculus, 5th Edition (Global Edition)",
        authors=["Jeffrey Perloff"],
        #isbn="9780743273565"
    )
    print(record)