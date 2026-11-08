"""validator 的書商後備：權威書目與 Google 都查不到時，依序查 TAAZE、誠品；
用 match_auditor 的判斷確認是同一本才算驗證成功（全部 monkeypatch，不連網、不打 LLM）。"""
from types import SimpleNamespace

import pytest

from library_agent.agents import validator as V
from library_agent.integrations.bookstore import StoreRecord
from library_agent.integrations.google_books import QuotaExceededError
from library_agent.state import BookCitation


def rec(title, authors=("黃宣範",), isbn=None, source="taaze"):
    return StoreRecord(title=title, authors=list(authors), isbn_13=isbn, publisher="", published="", source=source)


def cn(title="語言、社會與族群意識—台灣語言社會學研究", authors=("黃宣範",)):
    return BookCitation(course_id="C1", title=title, authors=list(authors), confidence=0.9)


class Stores:
    """假的書商：依查詢字串回傳預先放好的結果，並記錄每次查詢。"""

    def __init__(self):
        self.answers = {"taaze": {}, "eslite": {}}
        self.queries = []

    def search_for(self, name):
        def search(query):
            self.queries.append((name, query))
            return self.answers[name].get(query, [])
        return search


@pytest.fixture
def stores(monkeypatch):
    s = Stores()
    monkeypatch.setattr(V.taaze, "search", s.search_for("taaze"))
    monkeypatch.setattr(V.eslite, "search", s.search_for("eslite"))
    monkeypatch.setattr(V, "nla_lookup", lambda **kw: None)
    monkeypatch.setattr(V, "loc_lookup", lambda **kw: None)
    monkeypatch.setattr(V, "google_lookup", lambda **kw: None)
    monkeypatch.setattr(V._settings, "bookstore_sources", "taaze,eslite", raising=False)
    calls = []

    def llm(orig_title, orig_authors, cand_title, cand_authors):
        calls.append(cand_title)
        return True, "同一本"

    monkeypatch.setattr(V, "bookstore_llm", lambda: llm)
    s.llm_calls = calls
    return s


def test_taaze_hit_becomes_a_verified_book_with_pairing_fields(stores):
    full = "語言、社會與族群意識—台灣語言社會學研究"
    stores.answers["taaze"][full] = [rec("語言．社會與族群意識（台灣語言社會學研究）")]

    out = V._validate_chinese(cn(), low_confidence=False)

    assert out.verified is True and out.source == "taaze"
    assert out.canonical_title.startswith("語言．社會與族群意識") and out.canonical_authors == ["黃宣範"]
    assert (out.pairing_verdict, out.pairing_judge) == ("same", "llm") and out.pairing_reason


def test_eslite_is_tried_when_taaze_has_no_same_book(stores):
    stores.answers["eslite"]["被討厭的勇氣"] = [rec("被討厭的勇氣", ("岸見一郎",), "9789861371955", "eslite")]

    out = V._validate_chinese(cn("被討厭的勇氣", ("岸見一郎",)), low_confidence=False)

    assert out.verified and out.source == "eslite" and out.isbn_13 == "9789861371955"
    assert [q[0] for q in stores.queries] == ["taaze", "eslite"]                       # TAAZE 先，沒有才誠品


def test_citation_isbn_wins_over_the_bookstore_isbn(stores):
    stores.answers["eslite"]["被討厭的勇氣"] = [rec("被討厭的勇氣", isbn="9789861371955", source="eslite")]
    c = BookCitation(course_id="C1", title="被討厭的勇氣", isbn="9780306406157", confidence=0.9)

    assert V._validate_chinese(c, low_confidence=False).isbn_13 == "9780306406157"


def test_main_title_is_tried_only_after_the_full_title_finds_nothing(stores):
    stores.answers["taaze"]["語言、社會與族群意識"] = [rec("語言、社會與族群意識")]

    out = V._validate_chinese(cn(), low_confidence=False)

    assert out.verified and out.source == "taaze"
    assert stores.queries == [("taaze", "語言、社會與族群意識—台灣語言社會學研究"), ("taaze", "語言、社會與族群意識")]


def test_the_main_title_is_skipped_when_the_full_title_already_matched(stores):
    stores.answers["taaze"]["被討厭的勇氣"] = [rec("被討厭的勇氣", ("岸見一郎",))]

    V._validate_chinese(cn("被討厭的勇氣", ("岸見一郎",)), low_confidence=False)

    assert stores.queries == [("taaze", "被討厭的勇氣")]                                # 沒有主書名可補查，也不重複查


def test_no_store_hit_keeps_ncl_not_found(stores):
    out = V._validate_chinese(cn(), low_confidence=False)

    assert out.verified is False and out.source == "ncl_not_found"
    assert out.pairing_verdict is None


def test_english_books_also_fall_back_to_bookstores(stores):
    stores.answers["taaze"]["Impact Issues 3"] = [rec("Impact Issues 3", ("Day, Richard R.",))]
    c = BookCitation(course_id="C1", title="Impact Issues 3", authors=["Richard R. Day"], confidence=0.9)

    out = V._validate_english(c, low_confidence=False)

    assert out.verified and out.source == "taaze"


def test_english_miss_without_a_store_hit_stays_not_found(stores):
    out = V._validate_english(BookCitation(course_id="C1", title="Nope", confidence=0.9), low_confidence=False)

    assert out.verified is False and out.source == "not_found"


def test_quota_exceeded_never_reaches_bookstores(stores, monkeypatch):
    def quota(**kw):
        raise QuotaExceededError("quota")

    monkeypatch.setattr(V, "google_lookup", quota)

    out = V._validate_chinese(cn(), low_confidence=False)

    assert out.source == "quota_exceeded" and stores.queries == []                       # 之後會優先重驗，不先查書商


def test_empty_bookstore_sources_disables_the_fallback(stores, monkeypatch):
    monkeypatch.setattr(V._settings, "bookstore_sources", "")

    out = V._validate_chinese(cn(), low_confidence=False)

    assert out.source == "ncl_not_found" and stores.queries == []


def test_only_the_listed_sources_are_used_in_that_order(stores, monkeypatch):
    monkeypatch.setattr(V._settings, "bookstore_sources", "eslite")

    V._validate_chinese(cn(), low_confidence=False)

    assert {q[0] for q in stores.queries} == {"eslite"}


def test_authorless_generic_title_with_two_same_name_candidates_is_not_verified(stores):
    stores.answers["taaze"]["經濟學"] = [rec("經濟學", ("甲",)), rec("經濟學", ("乙",))]
    c = BookCitation(course_id="C1", title="經濟學", authors=[], confidence=0.9)

    out = V._validate_chinese(c, low_confidence=False)

    assert out.verified is False and out.source == "ncl_not_found"                      # 無從分辨是哪一本，不猜
    assert stores.llm_calls == []


def test_authorless_title_with_a_single_same_name_candidate_is_verified(stores):
    stores.answers["taaze"]["台灣史100件大事"] = [rec("台灣史100件大事（上）", ("李筱峰",))]
    c = BookCitation(course_id="C1", title="台灣史100件大事", authors=[], confidence=0.9)

    assert V._validate_chinese(c, low_confidence=False).verified is True


def test_llm_saying_different_keeps_the_book_unverified(stores, monkeypatch):
    stores.answers["taaze"]["Istanbul: City of the future"] = [rec("Istanbul: A Tale of Three Cities", ("Bettany Hughes",))]
    monkeypatch.setattr(V, "bookstore_llm", lambda: (lambda *a: (False, "不同作品")))
    c = BookCitation(course_id="C1", title="Istanbul: City of the future", authors=["Orhan"], confidence=0.9)

    assert V._validate_english(c, low_confidence=False).verified is False


def test_a_failing_store_is_treated_as_not_found(stores, monkeypatch):
    def boom(query):
        raise RuntimeError("改版了")

    monkeypatch.setattr(V.taaze, "search", boom)

    out = V._validate_chinese(cn(), low_confidence=False)

    assert out.verified is False and out.source == "ncl_not_found"                      # 不往外丟，流程照跑


def test_eslite_browser_is_closed_when_the_node_finishes(monkeypatch):
    closed = []
    monkeypatch.setattr(V.eslite, "close", lambda: closed.append(1))
    monkeypatch.setattr(V, "_pending_citations_query", lambda ids: V.select(V.Citation).where(V.Citation.id < 0))

    V.validator_node({"course_ids": None})

    assert closed == [1]


def test_saved_pairing_fields_keep_match_auditor_from_judging_again(isolated_db):
    from library_agent.agents import match_auditor as M
    from library_agent.db.models import Citation, Course, VerifiedBook as VB

    with isolated_db() as s:
        s.add(Course(course_id="C1", course_name="課", semester="114-1", source_file="t", raw_content="t"))
        s.flush()
        c = Citation(course_id="C1", title="被討厭的勇氣", authors="[]", is_required=True, raw_mention="x", confidence=0.9)
        s.add(c)
        s.flush()
        verified = V.VerifiedBook(citation=cn("被討厭的勇氣"), canonical_title="被討厭的勇氣", canonical_authors=["岸見一郎"],
                                  isbn_13=None, source="taaze", verified=True, pairing_verdict="same",
                                  pairing_judge="llm", pairing_reason="同一本")
        V._stage_save(s, verified, c.id)
        s.commit()

        row = s.query(VB).one()
        assert (row.source, row.pairing_verdict, row.pairing_judge, row.pairing_reason) == ("taaze", "same", "llm", "同一本")
        assert s.execute(M._pending_query(None, 0)).all() == []                              # 不會被挑去稽核
