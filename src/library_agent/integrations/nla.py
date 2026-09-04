"""國家圖書館 NCL / 全國圖書書目資訊網 NBINet 書目查詢（存在性驗證）。

NBINet 是 Ex Libris 系統，提供標準 SRU（Z39.50 over HTTP），格式與 alma.py / loc.py
相同的 MARCXML，因此直接複用同一套解析（245/100/700/020）。中文書用它做「存在性驗證」，
館藏（政大有無）仍由 librarian 查 Alma。不需要 Z39.50 原生協定。
"""
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

# NBINet（886NCL_NBINET）的 SRU 端點
_SRU_BASE = "https://nbinet.alma.exlibrisgroup.com/view/sru/886NCL_NBINET"
_SRU_PARAMS = {
    "version": "1.2",
    "operation": "searchRetrieve",
    "recordSchema": "marcxml",
    "maximumRecords": 1,
}
_TIMEOUT = 20.0

_SRW_NS = "http://www.loc.gov/zing/srw/"
_MARC_NS = "http://www.loc.gov/MARC21/slim"


@dataclass
class BookRecord:
    canonical_title: str
    canonical_authors: list[str]
    isbn_13: str | None
    source: str = "ncl"


def _parse_response(xml_text: str) -> BookRecord | None:
    root = ET.fromstring(xml_text)

    num_el = root.find(f"{{{_SRW_NS}}}numberOfRecords")
    if num_el is None or int(num_el.text or 0) == 0:
        return None

    marc = root.find(f".//{{{_MARC_NS}}}record")
    if marc is None:
        return None

    # 245 $a (主標) + $b (副標)
    title = ""
    f245 = marc.find(f"{{{_MARC_NS}}}datafield[@tag='245']")
    if f245 is not None:
        a = f245.findtext(f"{{{_MARC_NS}}}subfield[@code='a']") or ""
        b = f245.findtext(f"{{{_MARC_NS}}}subfield[@code='b']") or ""
        title = (a + " " + b).strip().rstrip("/").strip()

    if not title:
        return None

    # 100 $a (第一作者) + 700 $a (其他作者)
    authors = []
    f100 = marc.find(f"{{{_MARC_NS}}}datafield[@tag='100']")
    if f100 is not None:
        a = f100.findtext(f"{{{_MARC_NS}}}subfield[@code='a']")
        if a:
            authors.append(a.rstrip(",").strip())
    for f700 in marc.findall(f"{{{_MARC_NS}}}datafield[@tag='700']"):
        a = f700.findtext(f"{{{_MARC_NS}}}subfield[@code='a']")
        if a:
            authors.append(a.rstrip(",").strip())

    # 020 $a → 取 13 位 ISBN
    isbn_13 = None
    for f020 in marc.findall(f"{{{_MARC_NS}}}datafield[@tag='020']"):
        raw = f020.findtext(f"{{{_MARC_NS}}}subfield[@code='a']") or ""
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 13:
            isbn_13 = digits
            break

    return BookRecord(canonical_title=title, canonical_authors=authors, isbn_13=isbn_13)


def _query(cql: str) -> BookRecord | None:
    params = {**_SRU_PARAMS, "query": cql}
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.get(_SRU_BASE, params=params)
        if not response.text.lstrip().startswith("<?xml"):
            return None
        response.raise_for_status()
        return _parse_response(response.text)
    except Exception:
        return None


def _clean_title(title: str) -> str:
    # 去版次：第十版 / 第5版 / ，第十版 / （第五版）
    title = re.sub(r'[,，]?\s*第[〇一二三四五六七八九十百千\d]+[版刷]', '', title)
    # 去全形/半形括號內容
    title = re.sub(r'（[^）]*）', '', title)
    title = re.sub(r'\([^)]*\)', '', title)
    # 去內嵌年份：計算機概論2023
    title = re.sub(r'(?<!\d)20\d{2}(?!\d)', '', title)
    # 破折號副標：社會學概論——見樹又見林
    title = re.sub(r'——.*', '', title)
    # 全形冒號副標：策略新解：創造共享價值
    title = re.sub(r'：.*', '', title)
    return title.strip()


def lookup(
    title: str,
    authors: list[str] | None = None,
    isbn: str | None = None,
) -> BookRecord | None:
    # 1. ISBN（最準）
    if isbn:
        result = _query(f'alma.isbn="{isbn}"')
        if result:
            return result

    # 2. 書名
    result = _query(f'alma.title="{title}"')
    if result:
        return result

    # 3. 清理版次/副標後再查
    clean = _clean_title(title)
    if clean != title and clean:
        result = _query(f'alma.title="{clean}"')
        if result:
            return result

    return None


if __name__ == "__main__":
    tests = [
        ("社會學", None, None),
        ("", None, "9789570533439"),
    ]
    for title, authors, isbn in tests:
        r = lookup(title=title, authors=authors, isbn=isbn)
        if r:
            print(f"  OK   title={title!r} isbn={isbn!r}")
            print(f"       -> {r.canonical_title} / {r.canonical_authors} / isbn={r.isbn_13}")
        else:
            print(f"  MISS title={title!r} isbn={isbn!r}")
