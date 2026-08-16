import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import delete

from library_agent.config import get_settings
from library_agent.db.models import Citation
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.integrations.google_books import lookup as google_lookup
from library_agent.integrations.loc import lookup as loc_lookup
from library_agent.integrations.alma import check_holding
from library_agent.state import AgentState, BookCitation, VerifiedBook


def _is_chinese(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in text)

_settings = get_settings()
_MAX_WORKERS = 2


@dataclass
class _CacheEntry:
    canonical_title: str
    canonical_authors: list[str]
    isbn_13: str | None
    source: str


def _build_cache() -> dict[str, _CacheEntry]:
    """從 DB 讀出已成功驗證的書，以 isbn / title 為 key 建立查找表。
    同一本書被多門課引用時，第二筆以後直接命中 cache，不打 API。"""
    cache: dict[str, _CacheEntry] = {}

    with SessionLocal() as session:
        rows = session.execute(
            select(
                Citation.isbn,
                Citation.title,
                VerifiedBookDB.canonical_title,
                VerifiedBookDB.canonical_authors,
                VerifiedBookDB.isbn_13,
                VerifiedBookDB.source,
            )
            .join(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
            .where(VerifiedBookDB.verified == True)  # noqa: E712
        ).all()

    for row in rows:
        entry = _CacheEntry(
            canonical_title=row.canonical_title,
            canonical_authors=json.loads(row.canonical_authors) if row.canonical_authors else [],
            isbn_13=row.isbn_13,
            source=row.source,
        )
        if row.isbn:
            cache[f"isbn:{row.isbn}"] = entry
        cache[f"title:{row.title.strip().lower()}"] = entry
    return cache


def _validate_one(citation: BookCitation) -> VerifiedBook:
    low_confidence = citation.confidence < _settings.human_review_confidence_threshold

    if _is_chinese(citation.title):
        return _validate_alma(citation, low_confidence)
    else:
        return _validate_english(citation, low_confidence)


def _validate_english(citation: BookCitation, low_confidence: bool) -> VerifiedBook:
    kwargs = dict(title=citation.title, authors=citation.authors, isbn=citation.isbn)

    # LOC 優先（無配額限制）；伺服器不穩定時 loc_lookup 會回傳 None
    book_info = loc_lookup(**kwargs) or google_lookup(**kwargs)

    if book_info:
        return VerifiedBook(
            citation=citation,
            canonical_title=book_info.canonical_title,
            canonical_authors=book_info.canonical_authors,
            isbn_13=citation.isbn or book_info.isbn_13,
            source=book_info.source,
            verified=True,
            requires_human_review=low_confidence,
        )
    return VerifiedBook(
        citation=citation,
        canonical_title=citation.title,
        canonical_authors=citation.authors,
        isbn_13=citation.isbn,
        source="not_found",
        verified=False,
        requires_human_review=True,
    )


def _validate_alma(citation: BookCitation, low_confidence: bool) -> VerifiedBook:
    result = check_holding(isbn=citation.isbn, title=citation.title)
    if result.found:
        return VerifiedBook(
            citation=citation,
            canonical_title=citation.title,
            canonical_authors=citation.authors,
            isbn_13=citation.isbn or result.mms_id,
            source="alma",
            verified=True,
            requires_human_review=low_confidence,
        )
    # Alma 查不到：書可能存在但政大沒收藏，仍視為待確認
    return VerifiedBook(
        citation=citation,
        canonical_title=citation.title,
        canonical_authors=citation.authors,
        isbn_13=citation.isbn,
        source="alma_not_found",
        verified=False,
        requires_human_review=True,
    )


def _stage_save(session, verified: VerifiedBook, citation_id: int) -> None:
    """把單筆寫入暫存進「外部傳入的」session（先刪再寫），不 commit。
    由呼叫端（validator_node 主執行緒）統一分批 commit（B1）。"""
    session.execute(
        delete(VerifiedBookDB).where(VerifiedBookDB.citation_id == citation_id)
    )
    session.add(VerifiedBookDB(
        citation_id=citation_id,
        canonical_title=verified.canonical_title,
        canonical_authors=json.dumps(verified.canonical_authors, ensure_ascii=False),
        isbn_13=verified.isbn_13,
        source=verified.source,
        verified=verified.verified,
        requires_human_review=verified.requires_human_review,
    ))


def _process_one(
    c: Citation,
    cache: dict[str, _CacheEntry],
    done_ids: set[int],
) -> tuple[VerifiedBook | None, str | None, bool]:
    # 只跳過真的成功（verified=True）的紀錄；MISS / not_found 不在 done_ids 內，會重試。
    if c.id in done_ids:
        return None, None, False

    citation = BookCitation(
        course_id=c.course_id,
        title=c.title,
        authors=json.loads(c.authors) if c.authors else [],
        edition=c.edition,
        isbn=c.isbn,
        publisher=c.publisher,
        year=c.year,
        is_required=c.is_required,
        raw_mention=c.raw_mention or "",
        confidence=c.confidence,
    )

    # 先查 cache：同一本書已被其他 citation 驗證過，直接複用
    entry = None
    if c.isbn:
        entry = cache.get(f"isbn:{c.isbn}")
    if entry is None:
        entry = cache.get(f"title:{c.title.strip().lower()}")

    try:
        if entry:
            low_confidence = citation.confidence < _settings.human_review_confidence_threshold
            verified = VerifiedBook(
                citation=citation,
                canonical_title=entry.canonical_title,
                canonical_authors=entry.canonical_authors,
                isbn_13=entry.isbn_13,
                source=entry.source,
                verified=True,
                requires_human_review=low_confidence,
            )
            return verified, None, True   # True = cache hit
        else:
            verified = _validate_one(citation)
            return verified, None, False  # False = API call
    except Exception as e:
        return None, f"[validator] {c.id} {c.title}: {e}", False


def validator_node(state: AgentState) -> AgentState:
    errors: list[str] = []

    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        q = select(Citation)  # 產生一個「SELECT * FROM citation」的查詢物件
        if course_ids:
            q = q.where(Citation.course_id.in_(course_ids))
        citations = session.scalars(q).all()

        # A：一次撈出「已成功驗證」的 citation_id 集合，取代每筆一次的 _already_validated
        done_ids = set(session.scalars(
            select(VerifiedBookDB.citation_id).where(VerifiedBookDB.verified.is_(True))
        ).all())

    cache = _build_cache()
    print(f"  cache: {len(cache)} 筆已驗證書目可複用")

    total = len(citations)
    done = 0
    written = 0
    _BATCH = 200

    # B1：worker 只做 I/O（查 cache / 打 API）並回傳結果；DB 寫入全部收到主執行緒，
    # 用「同一個」session 分批 commit（Session 非 thread-safe，只有主執行緒能寫）。
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor, SessionLocal() as session:
        futures = {executor.submit(_process_one, c, cache, done_ids): c for c in citations}  # executor.submit(func, *args)：把函式丟進執行緒池執行，立刻回傳一個 Future 物件（代表「未來會完成的結果」）。

        for future in as_completed(futures):
            done += 1
            c = futures[future]
            try:
                verified, error, from_cache = future.result()
            except Exception as e:
                msg = f"[validator] {c.id} {c.title}: {e}"
                errors.append(msg)
                print(f"  ERROR [{done:>5}/{total}] {c.title} → {e}")
                continue

            if error:
                errors.append(error)
                print(f"  ERROR [{done:>5}/{total}] {c.title} → {error}")
            elif verified is None:
                print(f"  SKIP  [{done:>5}/{total}] {c.title}")
            else:
                # 主執行緒寫入（先刪再寫），每 _BATCH 筆才 commit 一次
                _stage_save(session, verified, c.id)
                written += 1
                if written % _BATCH == 0:
                    session.commit()

                if from_cache:
                    status = "CACHE"
                elif verified.verified:
                    status = "OK   "
                else:
                    status = "MISS "
                print(f"  {status} [{done:>5}/{total}] {c.title}")

        session.commit()  # 收尾：把最後不足一批的寫入 commit

    return {"errors": errors}


if __name__ == "__main__":
    _TEST_LIMIT = 10

    cache = _build_cache()

    with SessionLocal() as session:
        unvalidated = session.scalars(
            select(Citation)
            .outerjoin(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
            .where(VerifiedBookDB.id == None)  # noqa: E711
            .limit(_TEST_LIMIT)
        ).all()

    print(f"測試 {len(unvalidated)} 筆未驗證書目")
    with SessionLocal() as session:
        for c in unvalidated:
            verified, error, from_cache = _process_one(c, cache, set())
            if error:
                print(f"  ERROR  {c.title} → {error}")
            elif verified is None:
                print(f"  SKIP   {c.title}")
            else:
                _stage_save(session, verified, c.id)
                status = "CACHE" if from_cache else ("OK   " if verified.verified else "MISS ")
                print(f"  {status}  {c.title}")
        session.commit()
