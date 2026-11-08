"""書商後備驗證的只讀試跑：對現有的「查無此書」（verified=False、待審）跑 validator 的書商後備，
印出各來源命中幾本與明細。**不寫資料庫**——真的重跑 validator 會刪掉並重建這些書的館藏與建議。

用法：python experiments/bookstore_dryrun.py [N]     N＝只跑前 N 本（不重複書名），預設全部
會呼叫書商（每家約每秒一次）與 LLM（判斷是不是同一本），書名越多越久；先用 N=30 看看。
"""
import json
import sys
from collections import Counter

from sqlalchemy import select

from library_agent.agents import validator as V
from library_agent.db.models import Citation, VerifiedBook
from library_agent.db.session import SessionLocal
from library_agent.integrations import eslite
from library_agent.state import BookCitation


def not_found_citations(limit: int) -> list[BookCitation]:
    with SessionLocal() as s:
        rows = s.execute(
            select(Citation).join(VerifiedBook, VerifiedBook.citation_id == Citation.id)
            .where(VerifiedBook.verified.is_(False), VerifiedBook.review_status == "pending",
                   Citation.superseded_by_iteration.is_(None))
            .order_by(Citation.id)
        ).scalars().all()
        seen, out = set(), []
        for c in rows:
            title = (c.title or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            out.append(BookCitation(course_id=c.course_id, title=title, authors=json.loads(c.authors) if c.authors else [],
                                    isbn=c.isbn, confidence=c.confidence or 0.0))
        return out[:limit] if limit else out


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(f"書商來源：{V._settings.bookstore_sources or '（已關閉）'}")
    citations = not_found_citations(limit)
    print(f"查無此書（不重複書名）：{len(citations)} 本\n")
    hits = Counter()
    try:
        for i, c in enumerate(citations, 1):
            found = V._bookstore_fallback(c, low_confidence=False)
            if found is None:
                print(f"  [{i:>4}/{len(citations)}] 沒有  {c.title[:40]}")
                continue
            hits[found.source] += 1
            print(f"  [{i:>4}/{len(citations)}] {found.source:6s} {c.title[:30]}  →  {found.canonical_title[:30]} "
                  f"| {'、'.join(found.canonical_authors)[:20]} | isbn={found.isbn_13}")
            print(f"           判斷：{found.pairing_reason}")
    finally:
        eslite.close()
    total = sum(hits.values())
    print(f"\n命中 {total}/{len(citations)}（{total / max(len(citations), 1):.0%}）：{dict(hits)}")
    print("資料庫沒有任何改動。")


if __name__ == "__main__":
    main()
