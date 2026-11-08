import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy import delete

from library_agent.config import get_settings
from library_agent.db.models import Citation
from library_agent.db.models import HoldingCheck as HoldingCheckDB
from library_agent.db.models import Recommendation as RecommendationDB
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.agents.match_auditor import _norm, bookstore_llm, pick_same_book
from library_agent.integrations import eslite, sanmin, taaze
from library_agent.integrations.bookstore import StoreRecord, main_title
from library_agent.integrations.google_books import QuotaExceededError, lookup as google_lookup
from library_agent.integrations.loc import lookup as loc_lookup
from library_agent.integrations.nla import lookup as nla_lookup
from library_agent.state import AgentState, BookCitation, VerifiedBook


def _is_chinese(text: str) -> bool:
    return any("一" <= c <= "鿿" for c in text)

_settings = get_settings()
_MAX_WORKERS = 2
log = logging.getLogger(__name__)


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
        return _validate_chinese(citation, low_confidence)
    else:
        return _validate_english(citation, low_confidence)


def _quota_exceeded_result(citation: BookCitation) -> VerifiedBook:
    """Google 配額用完（今日）時的結果：不是真的查無此書，標成專屬 source 交人工審核，
    與 not_found/ncl_not_found 區隔；verified=False 讓配額恢復後重跑會自動 retry
    （且 validator 會優先撈 source='quota_exceeded' 的重驗）。"""
    return VerifiedBook(
        citation=citation,
        canonical_title=citation.title,
        canonical_authors=citation.authors,
        isbn_13=citation.isbn,
        source="quota_exceeded",
        verified=False,
        requires_human_review=True,
    )


_STORES = {"taaze": lambda: taaze.search, "sanmin": lambda: sanmin.search, "eslite": lambda: eslite.search}


def _bookstore_fallback(citation: BookCitation, low_confidence: bool) -> VerifiedBook | None:
    """原本的來源（NCL／LOC／Google）都查不到時，依 BOOKSTORE_SOURCES 的順序查書商。

    每家最多兩次：完整書名；沒有採用的候選且有主書名時再用主書名。判斷一律拿課綱的完整書名與作者，
    用 match_auditor 的 pick_same_book（字串規則＋LLM；LLM 失敗＝不同本）。判定同一本就當作驗證成功，
    稽核結果一併寫下（match_auditor 之後不會再判一次）。書商出任何錯都當作這家沒找到。"""
    names = [n.strip() for n in _settings.bookstore_sources.split(",") if n.strip()]
    if not names:
        return None
    queries = [citation.title.strip()]
    if (short := main_title(citation.title)):
        queries.append(short)
    llm = bookstore_llm()
    for name in names:
        search = _STORES.get(name)
        if search is None:
            continue
        for query in queries:
            try:
                records = _distinct_for_authorless(citation, search()(query))
                idx, reason = pick_same_book(citation.title, citation.authors,
                                             [(r.title, r.authors) for r in records], llm)
            except Exception as e:  # noqa: BLE001 — 書商或判斷出任何錯都當作這家沒找到，不能讓這本書被跳過或讓流程中斷
                log.warning("書商後備（%s）失敗，視為沒找到（%s）：%r", name, citation.title[:40], e)
                break
            if idx is not None:
                r = records[idx]
                return VerifiedBook(
                    citation=citation,
                    canonical_title=r.title,
                    canonical_authors=r.authors or citation.authors,
                    isbn_13=citation.isbn or r.isbn_13,
                    source=r.source,
                    verified=True,
                    requires_human_review=low_confidence,
                    pairing_verdict="same", pairing_judge="llm", pairing_reason=reason,
                )
    return None


def _distinct_for_authorless(citation: BookCitation, records: list[StoreRecord]) -> list[StoreRecord]:
    """課綱沒寫作者時，只靠書名無從分辨哪一本（通用教科書名如「經濟學」）：
    正規化後書名與原書名相同的候選有兩筆以上就全部不採用。"""
    if citation.authors:
        return records
    same = [r for r in records if _norm(r.title) == _norm(citation.title)]
    return [] if len(same) >= 2 else records


def _validate_english(citation: BookCitation, low_confidence: bool) -> VerifiedBook:
    kwargs = dict(title=citation.title, authors=citation.authors, isbn=citation.isbn)

    # LOC 優先（無配額限制）；伺服器不穩定時 loc_lookup 會回傳 None
    book_info = loc_lookup(**kwargs)
    if book_info is None:
        try:
            book_info = google_lookup(**kwargs)
        except QuotaExceededError:
            return _quota_exceeded_result(citation)

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
    if (store := _bookstore_fallback(citation, low_confidence)) is not None:
        return store
    return VerifiedBook(
        citation=citation,
        canonical_title=citation.title,
        canonical_authors=citation.authors,
        isbn_13=citation.isbn,
        source="not_found",
        verified=False,
        requires_human_review=True,
    )


def _validate_chinese(citation: BookCitation, low_confidence: bool) -> VerifiedBook:
    # 中文書優先走 NCL / NBINet 做「存在性驗證」（權威中文書目來源）；
    # NCL 查不到（冷門書/建檔延遲）再用 Google Books 補上（會佔 Google 每日配額）。
    # 「政大有沒有收藏」是館藏問題，由 librarian 另查 Alma。
    kwargs = dict(title=citation.title, authors=citation.authors, isbn=citation.isbn)
    book_info = nla_lookup(**kwargs)
    if book_info is None:
        try:
            book_info = google_lookup(**kwargs)
        except QuotaExceededError:
            return _quota_exceeded_result(citation)
    if book_info:
        return VerifiedBook(
            citation=citation,
            canonical_title=book_info.canonical_title or citation.title,
            canonical_authors=book_info.canonical_authors or citation.authors,
            isbn_13=citation.isbn or book_info.isbn_13,
            source=book_info.source,
            verified=True,
            requires_human_review=low_confidence,
        )
    # NCL 與 Google 都查不到：再查書商（TAAZE、誠品）；仍然沒有才是冷門書/建檔延遲，標為待確認交人工審核
    if (store := _bookstore_fallback(citation, low_confidence)) is not None:
        return store
    return VerifiedBook(
        citation=citation,
        canonical_title=citation.title,
        canonical_authors=citation.authors,
        isbn_13=citation.isbn,
        source="ncl_not_found",
        verified=False,
        requires_human_review=True,
    )


def _stage_save(session, verified: VerifiedBook, citation_id: int) -> None:
    """把單筆寫入暫存進「外部傳入的」session（先刪再寫），不 commit。
    由呼叫端（validator_node 主執行緒）統一分批 commit（B1）。

    MISS/not_found 的書每次重跑都會被 retry（見 _process_one），但下游 librarian
    對所有 verified_books（不分 verified 真偽）都會建 holding_check，
    recommender 再建 recommendation。若舊的 verified_books 已被下游引用，
    直接 DELETE 會撞 FK 約束，所以要比照 librarian._save_to_db 由下往上 cascade：
    recommendations → holding_checks → verified_books，才能刪掉舊列重寫。
    """
    old_id = session.scalar(
        select(VerifiedBookDB.id).where(VerifiedBookDB.citation_id == citation_id)
    )
    if old_id is not None:
        holding_ids = list(session.scalars(
            select(HoldingCheckDB.id).where(HoldingCheckDB.verified_book_id == old_id)
        ))
        if holding_ids:
            session.execute(
                delete(RecommendationDB).where(RecommendationDB.holding_id.in_(holding_ids))
            )
            session.execute(
                delete(HoldingCheckDB).where(HoldingCheckDB.verified_book_id == old_id)
            )
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
        review_status="pending" if verified.requires_human_review else None,
        pairing_verdict=verified.pairing_verdict,
        pairing_judge=verified.pairing_judge,
        pairing_reason=verified.pairing_reason,
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


def _pending_citations_query(course_ids: list[str] | None):
    """待驗證 citation 查詢；排除已被回饋迴路 superseded 的舊書。"""
    q = select(Citation).where(Citation.superseded_by_iteration.is_(None))
    if course_ids:
        q = q.where(Citation.course_id.in_(course_ids))
    return q


def validator_node(state: AgentState) -> AgentState:
    errors: list[str] = []

    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        citations = session.scalars(_pending_citations_query(course_ids)).all()

        # A：一次撈出「不需再處理」的 citation_id 集合，取代每筆一次的 _already_validated：
        #   已成功驗證(verified=True)，或已被人工審核處理(approved/rejected) → 都不重跑。
        done_ids = set(session.scalars(
            select(VerifiedBookDB.citation_id).where(
                or_(
                    VerifiedBookDB.verified.is_(True),
                    VerifiedBookDB.review_status.in_(("approved", "rejected")),
                )
            )
        ).all())

        # 上次 Google 配額爆掉的書（source='quota_exceeded'）優先重驗：配額有限，
        # 先把這些「只是沒配額、不是查無此書」的處理掉，避免它們排在新書後面餓死。
        quota_ids = set(session.scalars(
            select(VerifiedBookDB.citation_id).where(VerifiedBookDB.source == "quota_exceeded")
        ).all())

    citations = sorted(citations, key=lambda c: 0 if c.id in quota_ids else 1)

    cache = _build_cache()
    print(f"  cache: {len(cache)} 筆已驗證書目可複用")

    total = len(citations)
    done = 0
    written = 0
    _BATCH = 200

    try:
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
    finally:
        eslite.close()    # 誠品的瀏覽器（有開才會有東西可關）；順便解除這次執行內的停用

    return {"errors": errors, "discovery_iteration": state.get("discovery_iteration", 0) + 1}


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
