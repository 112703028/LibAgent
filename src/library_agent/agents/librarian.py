import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from library_agent.db.models import HoldingCheck as HoldingCheckDB
from library_agent.db.models import Recommendation as RecommendationDB
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.integrations.alma import check_holding
from library_agent.state import AgentState, BookCitation, HoldingCheck, HoldingStatus, VerifiedBook

_MAX_WORKERS = 4  # 對 NCCU Alma SRU 客氣一點；每本書 check_holding 內部最多還會查 3 次


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


def _process_one(vb: VerifiedBookDB) -> tuple[int, HoldingCheck | None, str | None]:
    """跑在 worker 執行緒：打 Alma、建 HoldingCheck，不碰 DB。
    vb 的 citation 已由 selectinload 預載，worker 內存取不會觸發 lazy DB 查詢。
    例外用 return 回報，不 raise，避免一筆拖垮整個 ThreadPoolExecutor。"""
    try:
        return vb.id, _check_one(vb), None
    except Exception as e:
        return vb.id, None, f"[librarian] {vb.id} {vb.canonical_title}: {e}"


def _stage_save(session, holding: HoldingCheck, verified_book_id: int) -> None:
    """把單筆寫入暫存進「外部傳入的」session，不 commit（由 librarian_node 主執行緒分批 commit）。
    覆寫前由下往上 cascade：recommendations → holding_checks，才能重寫。"""
    # 步驟 1：先查出這本書在 holding_checks 的 id
    existing_ids = list(session.scalars(
        select(HoldingCheckDB.id).where(HoldingCheckDB.verified_book_id == verified_book_id)
    ))
    if existing_ids:
        # 步驟 2：先把 recommendations 裡指向這些 id 的資料刪掉（請房客搬走）
        session.execute(
            delete(RecommendationDB).where(RecommendationDB.holding_id.in_(existing_ids))
        )
        # 步驟 3：再刪 holding_checks（拆房間）
        session.execute(
            delete(HoldingCheckDB).where(HoldingCheckDB.verified_book_id == verified_book_id)
        )
    # 步驟 4：寫入新的 holding_check
    session.add(HoldingCheckDB(
        verified_book_id=verified_book_id,
        status=holding.status.value,
        holdings_count=holding.holdings_count,
        alma_mms_id=holding.alma_mms_id,
    ))


def librarian_node(state: AgentState) -> AgentState:
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
    done = 0
    written = 0
    _BATCH = 100

    # worker 併發打 Alma（_process_one，不碰 DB）；主執行緒用單一 session
    # 依完成順序收結果、_stage_save 暫存、每 _BATCH 筆 commit 一次。
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor, SessionLocal() as session:
        futures = {executor.submit(_process_one, vb): vb for vb in verified_books}
        for future in as_completed(futures):
            done += 1
            vb = futures[future]
            vb_id, holding, error = future.result()
            label = f"[{done:>5}/{total}] {vb.canonical_title}"
            if error:
                errors.append(error)
                print(f"  ERROR                {label}  → {error}")
                continue
            _stage_save(session, holding, vb_id)
            written += 1
            print(f"  {holding.status.value:<20} {label}")
            if written % _BATCH == 0:
                session.commit()
        session.commit()  # 收尾

    return {"errors": errors}


if __name__ == "__main__":
    result = librarian_node({})
    from collections import Counter
    with SessionLocal() as session:
        statuses = session.scalars(select(HoldingCheckDB.status)).all()
    counts = Counter(statuses)
    print("\n館藏統計（DB 全量）：")
    for status, count in sorted(counts.items()):
        print(f"  {status:<20} {count} 筆")
    if result["errors"]:
        print(f"\n錯誤 {len(result['errors'])} 筆：")
        for e in result["errors"]:
            print(f"  {e}")
