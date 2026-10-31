"""人工審核區塊的讀取端：_load_data 帶回的審核分組、建議的「未驗證」旗標。

播種方式同 tests/test_dashboard_dedup.py：在開發庫播種專用資料、事後清掉（這裡只讀，不會改到真實資料）。
  配錯：一本書（TESTBOOK-RV-MM）被兩門課用到，分屬兩個學系（課甲 TESTRVM1、學系 A、60 人；課乙 TESTRVM2、學系 B、40 人）
  查無此書：一本書（TESTBOOK-RV-NF），一門課（TESTRVN1、學系 A、30 人），仍有一筆 high / 2 冊的採購建議
  已驗證：一本書（TESTBOOK-RV-OK），有採購建議，用來對照「未驗證」旗標
"""
import json

import pytest

from library_agent import dashboard
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)
from library_agent.db.session import SessionLocal

DEPT_A, DEPT_B = "TESTDEPT_RVA", "TESTDEPT_RVB"
COURSES = ["TESTRVM1", "TESTRVM2", "TESTRVN1", "TESTRVO1"]
T_MM, T_NF, T_OK = "TESTBOOK-RV-MM", "TESTBOOK-RV-NF", "TESTBOOK-RV-OK"


def _cleanup():
    with SessionLocal() as s:
        cit_ids = [i for (i,) in s.query(Citation.id).filter(Citation.course_id.in_(COURSES))]
        vb_ids = [i for (i,) in s.query(VerifiedBook.id).filter(VerifiedBook.citation_id.in_(cit_ids))]
        hc_ids = [i for (i,) in s.query(HoldingCheck.id).filter(HoldingCheck.verified_book_id.in_(vb_ids))]
        s.query(Recommendation).filter(Recommendation.holding_id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(HoldingCheck).filter(HoldingCheck.id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(VerifiedBook).filter(VerifiedBook.id.in_(vb_ids)).delete(synchronize_session=False)
        s.query(Citation).filter(Citation.id.in_(cit_ids)).delete(synchronize_session=False)
        s.query(Course).filter(Course.course_id.in_(COURSES)).delete(synchronize_session=False)
        s.commit()


def _seed_book(s, course_id, dept, enrolled, title, *, verified=True, verdict=None, review="pending",
               source="nla", recommend=None):
    """播種一門課的一個書目；recommend=(優先級, 冊數) 時一併建立採購建議與關聯列。"""
    s.add(Course(course_id=course_id, course_name=f"課-{course_id}", semester="9999", source_file="t",
                 raw_content="t", department=dept, enrolled_count=enrolled))
    s.flush()
    c = Citation(course_id=course_id, title=title, authors=json.dumps(["原作者"], ensure_ascii=False),
                 is_required=True, raw_mention="指定", confidence=0.9)
    s.add(c)
    s.flush()
    vb = VerifiedBook(citation_id=c.id, canonical_title=title, canonical_authors="[]", source=source,
                      verified=verified, pairing_verdict=verdict,
                      pairing_judge="llm" if verdict else None, pairing_reason="作者不同" if verdict else None,
                      review_status=review)
    s.add(vb)
    s.flush()
    hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
    s.add(hc)
    s.flush()
    if recommend:
        rec = Recommendation(course_id=course_id, holding_id=hc.id, priority=recommend[0],
                             suggested_copies=recommend[1], rationale="r", book_key=f"title:{title}")
        s.add(rec)
        s.flush()
        s.add(RecommendationCourse(recommendation_id=rec.id, course_id=course_id, citation_id=c.id,
                                   is_required=True, enrolled=enrolled))


@pytest.fixture
def review_world():
    _cleanup()
    with SessionLocal() as s:
        _seed_book(s, "TESTRVM1", DEPT_A, 60, T_MM, verdict="mismatch")
        _seed_book(s, "TESTRVM2", DEPT_B, 40, T_MM, verdict="mismatch")
        _seed_book(s, "TESTRVN1", DEPT_A, 30, T_NF, verified=False, source="ncl_not_found", recommend=("high", 2))
        _seed_book(s, "TESTRVO1", DEPT_A, 20, T_OK, review=None, recommend=("medium", 1))
        s.commit()
    yield
    _cleanup()


def _mine(groups):
    return [g for g in groups if g.title.startswith("TESTBOOK-RV")]


def test_load_data_exposes_review_groups_by_kind(review_world):
    review = dashboard._load_data()["review"]

    (mm,) = _mine(review["mismatch"])
    (nf,) = _mine(review["notfound"])
    assert (mm.items, mm.enrolled_total, mm.priority) == (2, 100, None)       # 配錯：已擋下、不在清單
    assert (nf.items, nf.priority, nf.copies) == (1, "high", 2)               # 查無此書：仍在清單裡
    assert not _mine(review["other"])


def test_review_groups_respect_the_department_filter(review_world):
    in_a = dashboard._load_data([DEPT_A])["review"]
    in_b = dashboard._load_data([DEPT_B])["review"]

    (mm_a,) = _mine(in_a["mismatch"])
    (mm_b,) = _mine(in_b["mismatch"])
    assert (mm_a.items, mm_a.enrolled_total) == (1, 60)                       # 只計所選學系的書目
    assert (mm_b.items, mm_b.enrolled_total) == (1, 40)
    assert _mine(in_a["notfound"]) and not _mine(in_b["notfound"])            # 學系 B 沒有查無此書的組，不報錯


def test_recrow_marks_only_unverified_books(review_world):
    recs = {r.title: r for r in dashboard._load_data()["recs"] if r.title.startswith("TESTBOOK-RV")}

    assert recs[T_NF].unverified is True
    assert recs[T_OK].unverified is False


def test_pending_list_for_kpi_and_export_is_unchanged(review_world):
    pending = dashboard._load_data()["pending"]

    assert all(len(row) == 7 for row in pending)                              # /export/pending 與 KPI 仍依賴這個形狀
