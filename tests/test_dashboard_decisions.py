"""採購建議帶出「不設上限的需求」、人的決定與採用冊數（讀取端）。

播種方式同 tests/test_dashboard_review.py：在開發庫播種專用資料、事後清掉（這裡只讀，不會改到真實資料）。
  大班（TESTBOOK-DC-BIG）：670 人、現有 1 冊 → 需求 26 本，系統建議被上限截斷成 5
  小班（TESTBOOK-DC-SMALL）：30 人、沒有館藏 → 需求 2 本，建議 2
"""
import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from library_agent import dashboard, main
from library_agent.db.models import (
    Citation, Course, HoldingCheck, PurchaseDecision, Recommendation, RecommendationCourse, VerifiedBook,
)
from library_agent.db.session import SessionLocal

COURSES = ["TESTDCA1", "TESTDCB1", "TESTDCC1"]
BIG, SMALL, LEGACY = "TESTBOOK-DC-BIG", "TESTBOOK-DC-SMALL", "TESTBOOK-DC-LEGACY"
KEY = lambda title: f"title:{title}"        # noqa: E731
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _cleanup():
    with SessionLocal() as s:
        s.query(PurchaseDecision).filter(PurchaseDecision.book_key.like("title:TESTBOOK-DC%")).delete(synchronize_session=False)
        cit_ids = [i for (i,) in s.query(Citation.id).filter(Citation.course_id.in_(COURSES))]
        vb_ids = [i for (i,) in s.query(VerifiedBook.id).filter(VerifiedBook.citation_id.in_(cit_ids))]
        hc_ids = [i for (i,) in s.query(HoldingCheck.id).filter(HoldingCheck.verified_book_id.in_(vb_ids))]
        s.query(Recommendation).filter(Recommendation.holding_id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(HoldingCheck).filter(HoldingCheck.id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(VerifiedBook).filter(VerifiedBook.id.in_(vb_ids)).delete(synchronize_session=False)
        s.query(Citation).filter(Citation.id.in_(cit_ids)).delete(synchronize_session=False)
        s.query(Course).filter(Course.course_id.in_(COURSES)).delete(synchronize_session=False)
        s.commit()


def _seed(s, course_id, title, enrolled, holdings, status, suggested, book_key):
    s.add(Course(course_id=course_id, course_name=f"課-{course_id}", semester="9999", source_file="t",
                 raw_content="t", enrolled_count=enrolled))
    s.flush()
    c = Citation(course_id=course_id, title=title, authors="[]", is_required=True, raw_mention="指定", confidence=0.9)
    s.add(c)
    s.flush()
    vb = VerifiedBook(citation_id=c.id, canonical_title=title, canonical_authors="[]", source="nla", verified=True)
    s.add(vb)
    s.flush()
    hc = HoldingCheck(verified_book_id=vb.id, status=status, holdings_count=holdings)
    s.add(hc)
    s.flush()
    rec = Recommendation(course_id=course_id, holding_id=hc.id, priority="medium", suggested_copies=suggested,
                         rationale="r", book_key=book_key)
    s.add(rec)
    s.flush()
    s.add(RecommendationCourse(recommendation_id=rec.id, course_id=course_id, citation_id=c.id,
                               is_required=True, enrolled=enrolled))


@pytest.fixture
def world():
    _cleanup()
    with SessionLocal() as s:
        _seed(s, "TESTDCA1", BIG, 670, 1, "owned_physical", 5, KEY(BIG))
        _seed(s, "TESTDCB1", SMALL, 30, 0, "missing", 2, KEY(SMALL))
        _seed(s, "TESTDCC1", LEGACY, 30, 0, "missing", 2, None)          # 舊格式：沒有 book_key
        s.commit()
    yield
    _cleanup()


def decide(book, copies, suggested_at):
    with SessionLocal() as s:
        s.add(PurchaseDecision(book_key=KEY(book), copies=copies, suggested_at_decision=suggested_at, decided_at=NOW))
        s.commit()


def row(book):
    (r,) = [x for x in dashboard._load_data()["recs"] if x.title == book]
    return r


# ---------- 需求與建議 ----------

def test_recrow_need_ignores_the_cap_and_suggestion_is_capped(world):
    r = row(BIG)

    assert (r.need, r.copies) == (26, 5)                       # 需求 26，被上限截斷成 5
    assert r.book_key == KEY(BIG)


def test_recrow_without_a_cut_has_need_equal_to_the_suggestion(world):
    r = row(SMALL)

    assert (r.need, r.copies) == (2, 2)


# ---------- 人的決定 ----------

def test_recrow_has_no_decision_by_default_and_final_is_the_suggestion(world):
    r = row(SMALL)

    assert r.decided is None and r.stale_from is None and r.final == r.copies == 2


def test_a_stored_decision_overrides_the_suggestion_in_final(world):
    decide(BIG, 12, suggested_at=5)

    r = row(BIG)

    assert (r.decided, r.final, r.copies, r.stale_from) == (12, 12, 5, None)      # 系統建議仍是 5


def test_a_zero_decision_means_final_zero(world):
    decide(SMALL, 0, suggested_at=2)

    r = row(SMALL)

    assert r.decided == 0 and r.final == 0                    # 0＝這次不買（不等於沒決定）


def test_stale_marker_when_the_suggestion_changed_since_the_decision(world):
    decide(BIG, 12, suggested_at=3)                           # 決定當時系統建議 3，現在是 5

    assert row(BIG).stale_from == 3


def test_no_stale_marker_when_the_suggestion_is_unchanged(world):
    decide(BIG, 12, suggested_at=5)

    assert row(BIG).stale_from is None


def test_legacy_recommendation_without_a_book_key_has_no_decision_slot(world):
    r = row(LEGACY)

    assert r.book_key is None and r.decided is None and r.final == r.copies


# ---------- KPI ----------

def test_kpi_copies_final_uses_decisions_and_copies_stays_the_systems_number(world):
    before = dashboard._load_data()["kpis"]
    decide(BIG, 12, suggested_at=5)
    decide(SMALL, 0, suggested_at=2)

    after = dashboard._load_data()["kpis"]

    assert after["copies"] == before["copies"]                              # 系統建議不受決定影響
    assert after["copies_final"] == before["copies_final"] + (12 - 5) + (0 - 2)


def test_adopted_total_is_the_sum_of_the_final_copies_of_the_listed_recommendations(world):
    # 與真實資料庫裡有沒有別人填的決定無關：採用冊數永遠等於列出的每本書的最終冊數加總
    decide(BIG, 12, suggested_at=5)

    data = dashboard._load_data()

    assert data["kpis"]["copies_final"] == sum(r.final for r in data["recs"])


# ---------- 報表與匯出 ----------

def test_report_rows_carry_need_decided_and_final(world):
    decide(BIG, 12, suggested_at=5)

    rows = {r["title"]: r for r in main._fetch_report_rows() if r["title"].startswith("TESTBOOK-DC")}

    assert (rows[BIG]["need_copies"], rows[BIG]["decided_copies"], rows[BIG]["final_copies"]) == (26, 12, 12)
    assert (rows[SMALL]["need_copies"], rows[SMALL]["decided_copies"], rows[SMALL]["final_copies"]) == (2, None, 2)


def test_export_csv_has_the_new_columns(world):
    decide(BIG, 12, suggested_at=5)

    text = TestClient(dashboard.app).get("/export/recommendations").text
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))

    header = rows[0]
    assert header[-4:-1] == ["需求冊數", "決定冊數", "最終採購數"] or {"需求冊數", "決定冊數", "最終採購數"} <= set(header)
    idx = {name: header.index(name) for name in ("書名", "冊數", "需求冊數", "決定冊數", "最終採購數")}
    big = next(r for r in rows if len(r) > idx["書名"] and r[idx["書名"]] == BIG)
    small = next(r for r in rows if len(r) > idx["書名"] and r[idx["書名"]] == SMALL)
    assert [big[idx[k]] for k in ("冊數", "需求冊數", "決定冊數", "最終採購數")] == ["5", "26", "12", "12"]
    assert [small[idx[k]] for k in ("冊數", "需求冊數", "決定冊數", "最終採購數")] == ["2", "2", "", "2"]    # 沒決定：決定欄留空
