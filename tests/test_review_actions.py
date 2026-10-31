"""review_actions：審核決定與撤銷（用獨立測試資料庫，絕不碰開發庫）。

動作語意（見設計文件第三步）：
  matched   買配對到的書 → approved，沒有採購書名
  original  買原書       → approved，採購書名＝原書名／原作者／（過檢查碼的）原書 ISBN
  custom    輸入書名     → approved，採購書名＝輸入的書名，作者留空，ISBN 選填
  none      都不買／捨棄 → rejected
"""
import builtins
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from library_agent import main
from library_agent import review_actions as RA
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, VerifiedBook,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def seed_book(S, n, *, title="原書名", citation_isbn=None, authors='["原作者"]', review="pending",
              holding=False, rec=False, matched_isbn="9780306406157"):
    """播種第 n 門課的一個書目。回傳 verified_book id。"""
    with S() as s:
        s.add(Course(course_id=f"A{n}", course_name=f"課-A{n}", semester="114-1", source_file="t",
                     raw_content="t", enrolled_count=30))
        s.flush()
        c = Citation(course_id=f"A{n}", title=title, authors=authors, isbn=citation_isbn, is_required=True,
                     raw_mention="指定", confidence=0.9)
        s.add(c)
        s.flush()
        vb = VerifiedBook(citation_id=c.id, canonical_title="配到的書", canonical_authors='["別人"]',
                          isbn_13=matched_isbn, source="nla", verified=True, review_status=review)
        s.add(vb)
        s.flush()
        if holding or rec:
            hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
            s.add(hc)
            s.flush()
            if rec:
                s.add(Recommendation(course_id=f"A{n}", holding_id=hc.id, priority="high", suggested_copies=1,
                                     rationale="r", book_key=f"k{n}"))
        s.commit()
        return vb.id


def do(S, ids, action, **kw):
    with S() as s:
        result = RA.decide(s, ids, action, now=NOW, **kw)
        s.commit()
        return result


def undo(S, ids):
    with S() as s:
        result = RA.undo(s, ids, now=NOW)
        s.commit()
        return result


def vb_of(S, vb_id):
    with S() as s:
        s.expire_on_commit = False
        return s.get(VerifiedBook, vb_id)


def fields(vb):
    return (vb.review_status, vb.review_choice, vb.purchase_title, vb.purchase_authors, vb.purchase_isbn)


# ---------- decide：各動作的語意 ----------

def test_matched_approves_without_purchase_fields(isolated_db):
    vb = seed_book(isolated_db, 1)

    result = do(isolated_db, [vb], "matched")

    assert result.changed == [vb] and result.skipped == []
    row = vb_of(isolated_db, vb)
    assert fields(row) == ("approved", "matched", None, None, None)
    assert row.reviewed_at == NOW and row.review_applied_at is None


def test_original_copies_the_citation_title_authors_and_a_valid_isbn(isolated_db):
    vb = seed_book(isolated_db, 1, title="Macroeconomics", citation_isbn="0-306-40615-2", authors='["Hall"]')

    do(isolated_db, [vb], "original")

    assert fields(vb_of(isolated_db, vb)) == (
        "approved", "original", "Macroeconomics", '["Hall"]', "9780306406157")      # ISBN-10 轉成 13 碼


def test_original_with_an_invalid_citation_isbn_leaves_purchase_isbn_null(isolated_db):
    vb = seed_book(isolated_db, 1, citation_isbn="991013282699705721")              # Alma 記錄編號不是 ISBN

    do(isolated_db, [vb], "original")

    row = vb_of(isolated_db, vb)
    assert row.purchase_isbn is None and row.buy_isbn is None        # 不沿用配到那本的 ISBN
    assert row.isbn_13 == "9780306406157"                            # 驗證證據原樣保留


def test_original_without_citation_authors_stores_an_empty_list(isolated_db):
    vb = seed_book(isolated_db, 1, authors=None)

    do(isolated_db, [vb], "original")

    assert vb_of(isolated_db, vb).purchase_authors == "[]"


def test_custom_stores_the_typed_title_and_normalized_isbn(isolated_db):
    vb = seed_book(isolated_db, 1)

    do(isolated_db, [vb], "custom", title="  我要買的書  ", isbn="0306406152")

    assert fields(vb_of(isolated_db, vb)) == ("approved", "custom", "我要買的書", "[]", "9780306406157")


def test_custom_without_an_isbn_keeps_it_null(isolated_db):
    vb = seed_book(isolated_db, 1)

    do(isolated_db, [vb], "custom", title="我要買的書", isbn="  ")

    assert vb_of(isolated_db, vb).purchase_isbn is None


def test_none_rejects(isolated_db):
    vb = seed_book(isolated_db, 1)

    do(isolated_db, [vb], "none")

    assert fields(vb_of(isolated_db, vb)) == ("rejected", "none", None, None, None)


# ---------- decide：範圍與保護 ----------

def test_decide_applies_to_every_item_of_a_group(isolated_db):
    ids = [seed_book(isolated_db, 1), seed_book(isolated_db, 2)]

    result = do(isolated_db, ids, "matched")

    assert sorted(result.changed) == sorted(ids)
    assert all(vb_of(isolated_db, i).review_status == "approved" for i in ids)


def test_decide_skips_books_that_are_no_longer_pending_and_never_overwrites_them(isolated_db):
    fresh = seed_book(isolated_db, 1)
    taken = seed_book(isolated_db, 2, review="approved")             # 別人（或命令列）已處理

    result = do(isolated_db, [fresh, taken, 999999], "none")

    assert result.changed == [fresh] and sorted(result.skipped) == sorted([taken, 999999])
    assert vb_of(isolated_db, taken).review_status == "approved"      # 沒被蓋掉


@pytest.mark.parametrize("action,kw", [
    ("custom", dict(title="   ")),                                   # 空白書名
    ("custom", dict(title="字" * 501)),                               # 超長
    ("custom", dict(title="x", isbn="123")),                          # ISBN 格式錯
    ("bogus", {}),                                                    # 未知動作
])
def test_decide_rejects_bad_input_and_writes_nothing(isolated_db, action, kw):
    vb = seed_book(isolated_db, 1)

    with isolated_db() as s:
        with pytest.raises(RA.ReviewError):
            RA.decide(s, [vb], action, now=NOW, **kw)
        s.rollback()

    assert fields(vb_of(isolated_db, vb)) == ("pending", None, None, None, None)


@pytest.mark.parametrize("ids", [[], list(range(1, 202))])
def test_decide_rejects_no_ids_or_too_many(isolated_db, ids):
    with isolated_db() as s:
        with pytest.raises(RA.ReviewError):
            RA.decide(s, ids, "none", now=NOW)


# ---------- undo ----------

def test_undo_resets_the_book_to_pending_and_clears_purchase_fields(isolated_db):
    vb = seed_book(isolated_db, 1, citation_isbn="0306406152")
    do(isolated_db, [vb], "original")

    result = undo(isolated_db, [vb])

    assert result.changed == [vb]
    row = vb_of(isolated_db, vb)
    assert fields(row) == ("pending", None, None, None, None)
    assert row.reviewed_at is None and row.review_applied_at is None


def test_undo_of_original_or_custom_deletes_its_holding_and_recommendation(isolated_db):
    vb = seed_book(isolated_db, 1, rec=True)
    do(isolated_db, [vb], "custom", title="我要買的書")

    undo(isolated_db, [vb])

    with isolated_db() as s:           # 用採購書名查的館藏不能留給之後改選「買配對到的書」
        assert s.scalar(select(HoldingCheck).where(HoldingCheck.verified_book_id == vb)) is None
        assert s.scalar(select(Recommendation).where(Recommendation.course_id == "A1")) is None


def test_undo_of_matched_keeps_the_holding(isolated_db):
    vb = seed_book(isolated_db, 1, holding=True)
    do(isolated_db, [vb], "matched")

    undo(isolated_db, [vb])

    with isolated_db() as s:
        assert s.scalar(select(HoldingCheck).where(HoldingCheck.verified_book_id == vb)) is not None


def test_undo_skips_books_without_a_decision(isolated_db):
    undecided = seed_book(isolated_db, 1)
    legacy = seed_book(isolated_db, 2, review="approved")             # 舊的（沒有 review_choice）核准

    result = undo(isolated_db, [undecided, legacy])

    assert result.changed == [] and sorted(result.skipped) == sorted([undecided, legacy])
    assert vb_of(isolated_db, legacy).review_status == "approved"


# ---------- 命令列審核走同一份邏輯 ----------

def test_cli_approve_and_reject_record_choice_and_time(isolated_db, monkeypatch):
    a = seed_book(isolated_db, 1)        # 課-A1：先出現
    b = seed_book(isolated_db, 2)        # 課-A2
    answers = iter(["a", "r"])
    monkeypatch.setattr(main, "SessionLocal", isolated_db)
    monkeypatch.setattr(builtins, "input", lambda *_: next(answers))

    main.review()

    ra, rb = vb_of(isolated_db, a), vb_of(isolated_db, b)
    assert (ra.review_status, ra.review_choice) == ("approved", "matched") and ra.reviewed_at is not None
    assert (rb.review_status, rb.review_choice) == ("rejected", "none") and rb.reviewed_at is not None
