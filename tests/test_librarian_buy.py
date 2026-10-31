"""librarian 讀「採購書名」：查館藏用 buy_isbn / buy_title，並能對指定的書重查（套用審核結果用）。"""
from types import SimpleNamespace

from sqlalchemy import select

from library_agent.agents import librarian as L
from library_agent.db.models import Citation, Course, HoldingCheck, VerifiedBook

FOUND3 = SimpleNamespace(found=True, has_ebook=False, physical_count=3, mms_id="999")


def detached_vb(**kw):
    """不進資料庫的 ORM 物件，足夠讓 _check_one 讀欄位。"""
    vb = VerifiedBook(canonical_title="配到的書", canonical_authors='["別人"]', isbn_13="9780306406157",
                      source="nla", verified=True, requires_human_review=False, **kw)
    vb.citation = Citation(course_id="C", title="原書名", authors="[]", is_required=True, raw_mention="x",
                           confidence=0.9)
    return vb


def test_check_one_queries_alma_with_the_purchase_title_and_isbn(monkeypatch):
    seen = {}
    monkeypatch.setattr(L, "check_holding", lambda isbn, title: seen.update(isbn=isbn, title=title) or FOUND3)

    L._check_one(detached_vb(purchase_title="我要買的", purchase_authors="[]", purchase_isbn="9780199296095"))

    assert seen == {"isbn": "9780199296095", "title": "我要買的"}


def test_check_one_uses_no_isbn_when_the_purchase_title_has_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(L, "check_holding", lambda isbn, title: seen.update(isbn=isbn, title=title) or FOUND3)

    L._check_one(detached_vb(purchase_title="我要買的", purchase_authors="[]", purchase_isbn=None))

    assert seen == {"isbn": None, "title": "我要買的"}        # 不沿用配到那本的 ISBN


def test_check_one_without_a_purchase_title_still_uses_the_matched_book(monkeypatch):
    seen = {}
    monkeypatch.setattr(L, "check_holding", lambda isbn, title: seen.update(isbn=isbn, title=title) or FOUND3)

    L._check_one(detached_vb())

    assert seen == {"isbn": "9780306406157", "title": "配到的書"}


def seed(S, n, purchase_title):
    with S() as s:
        s.add(Course(course_id=f"L{n}", course_name=f"課-L{n}", semester="114-1", source_file="t", raw_content="t"))
        s.flush()
        c = Citation(course_id=f"L{n}", title="原書名", authors="[]", is_required=True, raw_mention="x", confidence=0.9)
        s.add(c)
        s.flush()
        vb = VerifiedBook(citation_id=c.id, canonical_title="配到的書", canonical_authors="[]",
                          isbn_13=None, source="nla", verified=True, review_status="approved",
                          review_choice="custom", purchase_title=purchase_title, purchase_authors="[]")
        s.add(vb)
        s.flush()
        s.add(HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0))
        s.commit()
        return vb.id


def test_recheck_holdings_rewrites_changed_holdings_and_reports_failures(isolated_db, monkeypatch):
    good, bad = seed(isolated_db, 1, "好書"), seed(isolated_db, 2, "壞掉的書")

    def fake(isbn, title):
        if title == "壞掉的書":
            raise RuntimeError("Alma 掛了")
        return FOUND3

    monkeypatch.setattr(L, "SessionLocal", isolated_db)
    monkeypatch.setattr(L, "check_holding", fake)

    result = L.recheck_holdings([good, bad])

    assert result.written == 1 and result.failed == [bad] and len(result.errors) == 1
    with isolated_db() as s:
        row = s.scalar(select(HoldingCheck).where(HoldingCheck.verified_book_id == good))
        assert (row.status, row.holdings_count, row.alma_mms_id) == ("owned_physical", 3, "999")
        old = s.scalar(select(HoldingCheck).where(HoldingCheck.verified_book_id == bad))
        assert old.status == "missing"                 # 失敗的那本保持原樣


def test_recheck_holdings_with_no_ids_does_nothing(isolated_db, monkeypatch):
    monkeypatch.setattr(L, "SessionLocal", isolated_db)

    result = L.recheck_holdings([])

    assert (result.written, result.failed, result.errors) == (0, [], [])
