import json

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from library_agent.db.models import HoldingCheck as HoldingCheckDB
from library_agent.db.models import Recommendation as RecommendationDB
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.integrations.alma import check_holding
from library_agent.state import AgentState, BookCitation, HoldingCheck, HoldingStatus, VerifiedBook


def _to_status(result) -> HoldingStatus:
    if not result.found:
        return HoldingStatus.MISSING   # 完全沒有這本書
    if result.has_ebook:
        return HoldingStatus.OWNED_EBOOK  # 電子書優先回傳 
    if result.physical_count > 0:
        return HoldingStatus.OWNED_PHYSICAL
    return HoldingStatus.PARTIAL  # found = True（書目存在）、沒有電子書、實體冊數 = 0。這種情況在 Alma 裡可能是書目記錄存在但館藏已全數銷毀或遺失，用 PARTIAL 表示「有記錄但狀態異常」。


def _check_one(vb: VerifiedBookDB) -> HoldingCheck:
    result = check_holding(isbn=vb.isbn_13, title=vb.canonical_title)
    status = _to_status(result)

    citation = vb.citation   # 因為VerifiedBook, Citation 有建立關聯，所以可以直接從 VerifiedBookDB 的 citation 欄位拿到對應的 CitationDB 物件

    # 把 DB 物件轉成 Pydantic 物件
    book_citation = BookCitation(
        course_id=citation.course_id,
        title=citation.title,
        authors=json.loads(citation.authors) if citation.authors else [],
        edition=citation.edition,
        isbn=citation.isbn,
        publisher=citation.publisher,
        year=citation.year,
        is_required=citation.is_required,
        raw_mention=citation.raw_mention or "",
        confidence=citation.confidence,
    )
    verified = VerifiedBook(
        citation=book_citation,
        canonical_title=vb.canonical_title,
        canonical_authors=json.loads(vb.canonical_authors) if vb.canonical_authors else [],
        isbn_13=vb.isbn_13,
        source=vb.source,
        verified=vb.verified,
        requires_human_review=vb.requires_human_review,
    )
    return HoldingCheck(
        book=verified,  # 從 DB 重建的 VerifiedBook（包含 BookCitation）
        status=status,  # Alma API 查詢結果轉換的 enum
        holdings_count=result.physical_count,  # Alma API 回傳的 AVA $f（總冊數）
        alma_mms_id=result.mms_id,             # Alma API 回傳的 MARC 001（書目 ID）
    )


def _save_to_db(holding: HoldingCheck, verified_book_id: int) -> None:
    with SessionLocal() as session:
        existing_ids = list(session.scalars(
            select(HoldingCheckDB.id).where(HoldingCheckDB.verified_book_id == verified_book_id)
        ))
        if existing_ids:
            session.execute(
                delete(RecommendationDB).where(RecommendationDB.holding_id.in_(existing_ids))
            )
            session.execute(
                delete(HoldingCheckDB).where(HoldingCheckDB.verified_book_id == verified_book_id)
            )
        session.add(HoldingCheckDB(
            verified_book_id=verified_book_id,
            status=holding.status.value,
            holdings_count=holding.holdings_count,
            alma_mms_id=holding.alma_mms_id,
        ))
        session.commit()


def librarian_node(state: AgentState) -> AgentState:
    all_holdings: list[HoldingCheck] = []
    errors: list[str] = []

    # 從資料庫一次讀出所有 verified_books。.all() 把 iterator 一次全部轉成 list，存在記憶體裡。這樣後面就可以先關掉 with session，迴圈再慢慢處理，不會佔著資料庫連線。
    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        from library_agent.db.models import Citation as CitationDB
        q = select(VerifiedBookDB).options(selectinload(VerifiedBookDB.citation))
        if course_ids:
            q = q.join(CitationDB, CitationDB.id == VerifiedBookDB.citation_id).where(
                CitationDB.course_id.in_(course_ids)
            )
        verified_books = session.scalars(q).all()

    total = len(verified_books)
    for i, vb in enumerate(verified_books, 1):
        label = f"[{i:>4}/{total}] {vb.canonical_title}"
        try:
            holding = _check_one(vb)
            _save_to_db(holding, vb.id)
            print(f"  {holding.status.value:<20} {label}")
            all_holdings.append(holding)
        except Exception as e:
            errors.append(f"[librarian] {vb.id} {vb.canonical_title}: {e}")
            print(f"  ERROR                {label}  → {e}")

    return {"holdings": all_holdings, "errors": errors}


if __name__ == "__main__":
    result = librarian_node({})
    from collections import Counter
    counts = Counter(h.status.value for h in result["holdings"])
    print("\n館藏統計：")
    for status, count in sorted(counts.items()):
        print(f"  {status:<20} {count} 筆")
    if result["errors"]:
        print(f"\n錯誤 {len(result['errors'])} 筆：")
        for e in result["errors"]:
            print(f"  {e}")
