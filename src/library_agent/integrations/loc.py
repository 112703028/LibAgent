import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

_SRU_BASE = "https://lx2.loc.gov/sru/Voyager"
_SRU_PARAMS = {"version": "1.1", "operation": "searchRetrieve", "maximumRecords": 1}
_TIMEOUT = 20.0

_SRW_NS = "http://www.loc.gov/zing/srw/"
_MARC_NS = "http://www.loc.gov/MARC21/slim"


@dataclass
class BookRecord:
    canonical_title: str
    canonical_authors: list[str]
    isbn_13: str | None
    source: str = "loc"


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
        if not response.text.startswith("<?xml"):
            return None
        response.raise_for_status()
        return _parse_response(response.text)
    except Exception:
        return None


def _clean_title(title: str) -> str:
    title = re.sub(r",?\s*\d+\w*\s+[Ee]d(?:ition)?\.?.*", "", title)
    title = re.sub(r"\s*\(.*?\)", "", title)
    return title.strip()


def _strip_subtitle(title: str) -> str:
    return re.sub(r"\s*:.*", "", title).strip()


def lookup(
    title: str,
    authors: list[str] | None = None,
    isbn: str | None = None,
) -> BookRecord | None:
    first_author = authors[0].split(",")[0].strip() if authors else None

    # 1. ISBN（不需要作者）
    if isbn:
        result = _query(f'bath.isbn="{isbn}"')
        if result:
            return result

    # LOC 館藏龐雜，書名單獨查很容易誤中不相關書目，以下查詢都要求同時有作者
    if not first_author:
        return None

    # 2. 書名 + 作者
    result = _query(f'dc.title="{title}" and dc.creator="{first_author}"')
    if result:
        return result

    # 3. 清理版次後再查（處理 "Management, 14th ed."）
    clean = _clean_title(title)
    if clean != title and clean:
        result = _query(f'dc.title="{clean}" and dc.creator="{first_author}"')
        if result:
            return result

    # 4. 去掉副標（處理 "World Politics: Trend & Transformation"）
    short = _strip_subtitle(clean if (clean != title and clean) else title)
    if short not in {title, clean} and short:
        result = _query(f'dc.title="{short}" and dc.creator="{first_author}"')
        if result:
            return result

    return None


if __name__ == "__main__":
    tests = [
        ("World Politics: Trend & Transformation", ["Kegley, Charles W."], None),
        ("Management, 14th ed.", ["Robbins, Stephen"], None),
        ("A History of Corporate Social Responsibility: Concepts and Practices", ["Carroll, A. B."], None),
    ]
    for title, authors, isbn in tests:
        r = lookup(title=title, authors=authors, isbn=isbn)
        if r:
            print(f"  OK   {title}")
            print(f"       -> {r.canonical_title} / {r.canonical_authors} / isbn={r.isbn_13}")
        else:
            print(f"  MISS {title}")
