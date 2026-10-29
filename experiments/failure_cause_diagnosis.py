"""首驗失敗書的成因診斷：把失敗書分成三類，量化「驗證器是瓶頸」的成因結構。
中英文都診斷（中文 ncl_not_found 是失敗主體，481 本；英文 not_found 49 本）。

每本失敗書判成三類之一：
  A. 修正後救回（rescued）：用修正後的 lookup 重查（英文 google/loc、中文 nla→google），
     現在查得到（含作者比對）→ 屬「查詢邏輯缺陷」，已被本次卷號/副標修正解決
  B. 查詢缺陷未解（query-defect-remaining）：修正後 lookup 仍 miss，但寬鬆查詢
     （書名關鍵字、無精確運算子）在 Google Books 找得到高度相似書名
     → 書存在，但精確比對仍抓不到（更複雜的書名變體）
  C. 覆蓋缺口（coverage-gap）：連寬鬆查詢都找不到相似書名 → 資料源沒收錄

⚠️ 中文探測限制：中文「是否真存在」用 Google Books 中文 loose query 判斷，但 Google Books
   對繁中書收錄不全，故中文的 C 類（覆蓋缺口）會被「高估」——部分書其實存在於 NCL/書商
   但 Google 查不到。論文須註明此限制；中文 C 類是「上限估計」。

用法：
    python -u experiments/failure_cause_diagnosis.py
"""
import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

# Windows 終端預設 cp950，print 含重音字元（é 等）會 UnicodeEncodeError，強制 UTF-8 輸出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
from sqlalchemy import select

from library_agent.config import get_settings
from library_agent.db.models import Citation, VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.integrations.google_books import lookup as gb_lookup
from library_agent.integrations.loc import lookup as loc_lookup
from library_agent.integrations.nla import lookup as nla_lookup
from library_agent.integrations import nla as _nla

_KEY = get_settings().google_books_api_key
_H = {"User-Agent": "Mozilla/5.0"}


def _is_chinese(t: str) -> bool:
    return any("一" <= c <= "鿿" for c in t)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", s.lower())


def _loose_exists(title: str) -> str | None:
    """寬鬆查詢（無精確運算子）：書名丟 Google Books，回傳最相似候選書名或 None。
    用書名核心（去卷號/副標）正規化子字串重疊判斷高度相似。"""
    params = {"q": title, "maxResults": 3, "fields": "items/volumeInfo"}
    if _KEY:
        params["key"] = _KEY
    try:
        r = httpx.get("https://www.googleapis.com/books/v1/volumes", params=params, timeout=15, headers=_H)
        items = r.json().get("items", [])
    except Exception:
        return None
    core = _norm(re.sub(r"[,:：].*", "", title))
    if len(core) < 3:  # 核心太短，重疊判斷不可靠，直接視為找不到
        return None
    for it in items:
        cand = it.get("volumeInfo", {}).get("title", "")
        cn = _norm(cand)
        if core and (core in cn or cn in core):
            return cand
    return None


def _ncl_loose_exists(title: str) -> str | None:
    """中文書的寬鬆存在性探測：查 NCL/NBINet，只比書名相似（不比作者），回傳最相似候選或 None。
    取代原本用 Google Books 探測中文（Google 對繁中收錄不全，會把 NCL 有的書誤判成覆蓋缺口）。"""
    core = _norm(re.sub(r"[,:：].*", "", title))
    if len(core) < 3:
        return None
    try:
        records = _nla._query(f'alma.title="{title}"')
        if not records:  # 原書名查無，退回清理版次/副標後再查
            clean = _nla._clean_title(title)
            if clean and clean != title:
                records = _nla._query(f'alma.title="{clean}"')
    except Exception:
        return None
    for rec in records:
        cn = _norm(rec.canonical_title)
        if core and (core in cn or cn in core):
            return rec.canonical_title
    return None


def _diagnose(title: str, authors: list[str], is_zh: bool) -> tuple[str, str]:
    # A. 修正後 lookup 是否救回
    if is_zh:
        r = nla_lookup(title=title, authors=authors, isbn=None)
        if r is None:
            try:
                r = gb_lookup(title=title, authors=authors, isbn=None)
            except Exception:
                r = None
    else:
        r = gb_lookup(title=title, authors=authors, isbn=None)
        if r is None:
            r = loc_lookup(title=title, authors=authors, isbn=None)
    if r is not None:
        return "A_rescued", f"修正後查得：{r.canonical_title[:36]} (isbn={r.isbn_13})"

    # B/C. 寬鬆探測：中文用 NCL 為主探針（Google 收錄不全會高估覆蓋缺口），英文用 Google/LOC
    if is_zh:
        loose = _ncl_loose_exists(title)
        src = "NCL"
        if loose is None:  # NCL 也查無，再用 Google 輔助（保守：多一層存在性確認）
            loose = _loose_exists(title)
            src = "Google(輔)"
    else:
        loose = _loose_exists(title)
        src = "Google/LOC"
    if loose:
        return "B_query_defect", f"寬鬆查得相似（{src}）：{loose[:32]}"
    return "C_coverage_gap", "NCL/Google 寬鬆查詢皆無相似書名"


def _run(books, is_zh, label, detail_rows):
    """診斷每本書並累計；每筆明細 append 進 detail_rows（供寫 CSV 存檔佐證）。"""
    counts = {"A_rescued": 0, "B_query_defect": 0, "C_coverage_gap": 0}
    lang = "zh" if is_zh else "en"
    print(f"\n----- {label}（{len(books)} 本）-----", flush=True)
    for i, (title, authors) in enumerate(books, 1):
        cat, note = _diagnose(title, authors, is_zh)
        counts[cat] += 1
        detail_rows.append({"lang": lang, "idx": i, "category": cat,
                            "title": title, "authors": json.dumps(authors, ensure_ascii=False),
                            "note": note})
        print(f"[{i:>3}] {cat:16} {title[:38]} | {note}", flush=True)
    return counts


def _report(label, counts, note=""):
    total = sum(counts.values()) or 1
    print(f"\n【{label}】共 {sum(counts.values())} 本 {note}", flush=True)
    print(f"  A 修正後救回（查詢缺陷已解）: {counts['A_rescued']:>3} ({counts['A_rescued']/total*100:.1f}%)", flush=True)
    print(f"  B 查詢缺陷未解（書存在仍 miss）: {counts['B_query_defect']:>3} ({counts['B_query_defect']/total*100:.1f}%)", flush=True)
    print(f"  C 覆蓋缺口（源未收錄）: {counts['C_coverage_gap']:>3} ({counts['C_coverage_gap']/total*100:.1f}%)", flush=True)
    q = counts['A_rescued'] + counts['B_query_defect']
    print(f"  → 查詢相關 A+B（改善查詢即可解）: {q} ({q/total*100:.1f}%)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-zh", type=int, default=0, help="中文抽樣上限（0=全部 481）")
    ap.add_argument("--limit-en", type=int, default=0, help="英文抽樣上限（0=全部 49）")
    args = ap.parse_args()

    with SessionLocal() as s:
        rows = s.execute(
            select(Citation.title, Citation.authors, VerifiedBookDB.source)
            .join(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
            .where(VerifiedBookDB.source.in_(("not_found", "ncl_not_found")))
        ).all()

    zh, en = [], []
    for t, a, src in rows:
        authors = json.loads(a) if a else []
        (zh if _is_chinese(t) else en).append((t, authors))
    # 隨機抽樣（固定 seed 可重現），避免只取到開頭集中的重複書目
    random.seed(42)
    if args.limit_zh and args.limit_zh < len(zh):
        zh = random.sample(zh, args.limit_zh)
    if args.limit_en and args.limit_en < len(en):
        en = random.sample(en, args.limit_en)

    print(f"失敗書總數：中文 {len(zh)} 本、英文 {len(en)} 本", flush=True)

    detail_rows: list[dict] = []
    en_counts = _run(en, False, "英文 not_found", detail_rows)
    zh_counts = _run(zh, True, "中文 ncl_not_found", detail_rows)

    # 寫明細 CSV 存檔（可重現佐證：論文表 2 的 T1/T2/T3 分類逐本結果）
    out_path = Path(__file__).resolve().parent / "failure_cause_diagnosis_result.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["lang", "idx", "category", "title", "authors", "note"])
        writer.writeheader()
        writer.writerows(detail_rows)
    print(f"\n明細已寫入 {out_path}", flush=True)

    print("\n" + "=" * 58, flush=True)
    print("首驗失敗成因分類結果", flush=True)
    print("=" * 58, flush=True)
    # 英文用 Google Books/LOC 探測；中文以 NCL 為主探針、Google 輔助（見 _diagnose）
    _report("英文", en_counts, "（Google Books/LOC 探測）")
    _report("中文", zh_counts, "（NCL 為主探針、Google 輔助；C 類為上限估計）")


if __name__ == "__main__":
    main()
