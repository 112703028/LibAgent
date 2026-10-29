"""一本書一筆的讀取端：dashboard、報表、匯出。

播種方式同 tests/test_dashboard_filters.py：在開發庫播種專用資料、事後清掉（這裡只讀，不會改到真實資料）。
一本書（TESTBOOK-DD）被兩門課用到，分屬兩個學系：
  課甲 TESTDDA（學系 TESTDEPT_DDA，參考，60 人）、課乙 TESTDDB（學系 TESTDEPT_DDB，指定，40 人）
建議只有一筆，全校合計 3 本；關聯表記錄兩門課各自的類別與人數。
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from library_agent import dashboard, main
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)
from library_agent.db.session import SessionLocal

DEPT_A, DEPT_B = "TESTDEPT_DDA", "TESTDEPT_DDB"
COURSE_A, COURSE_B = "TESTDDA", "TESTDDB"
TITLE = "TESTBOOK-DD"


def _has(text: str, needle: str) -> bool:
    # 頁面有 1 MB；直接 assert "x" in text 失敗時 pytest 會對整份 HTML 做差異比對，慢到像卡死
    return needle in text


def _cleanup():
    ids = [COURSE_A, COURSE_B]
    with SessionLocal() as s:
        cit_ids = [i for (i,) in s.query(Citation.id).filter(Citation.course_id.in_(ids))]
        vb_ids = [i for (i,) in s.query(VerifiedBook.id).filter(VerifiedBook.citation_id.in_(cit_ids))]
        hc_ids = [i for (i,) in s.query(HoldingCheck.id).filter(HoldingCheck.verified_book_id.in_(vb_ids))]
        s.query(Recommendation).filter(Recommendation.holding_id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(HoldingCheck).filter(HoldingCheck.id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(VerifiedBook).filter(VerifiedBook.id.in_(vb_ids)).delete(synchronize_session=False)
        s.query(Citation).filter(Citation.id.in_(cit_ids)).delete(synchronize_session=False)
        s.query(Course).filter(Course.course_id.in_(ids)).delete(synchronize_session=False)
        s.commit()


@pytest.fixture
def shared_book():
    _cleanup()
    with SessionLocal() as s:
        parts = []
        for cid, dept, required, enrolled in ((COURSE_A, DEPT_A, False, 60), (COURSE_B, DEPT_B, True, 40)):
            s.add(Course(course_id=cid, course_name=f"課-{cid}", semester="9999", source_file="t",
                         raw_content="t", department=dept, enrolled_count=enrolled))
            s.flush()
            c = Citation(course_id=cid, title=TITLE, authors="[]", is_required=required,
                         raw_mention="指定" if required else "參考", confidence=0.9)
            s.add(c)
            s.flush()
            vb = VerifiedBook(citation_id=c.id, canonical_title=TITLE, canonical_authors="[]",
                              isbn_13="9780306406157", source="nla", verified=True)
            s.add(vb)
            s.flush()
            hc = HoldingCheck(verified_book_id=vb.id, status="owned_physical", holdings_count=1)
            s.add(hc)
            s.flush()
            parts.append((cid, c, hc, required, enrolled))
        rep_course, _, rep_hc, _, _ = parts[0]
        rec = Recommendation(course_id=rep_course, holding_id=rep_hc.id, priority="medium",
                             suggested_copies=3, rationale="合計 100 人", book_key="isbn:9780306406157")
        s.add(rec)
        s.flush()
        for cid, c, _, required, enrolled in parts:
            s.add(RecommendationCourse(recommendation_id=rec.id, course_id=cid, citation_id=c.id,
                                       is_required=required, enrolled=enrolled))
        s.commit()
    yield
    _cleanup()


def _rows(data):
    return [r for r in data["recs"] if r.title == TITLE]


def test_recs_are_one_row_per_book_with_all_courses(shared_book):
    (row,) = _rows(dashboard._load_data([DEPT_A, DEPT_B]))

    assert {(c.name, c.key) for c in row.courses} == {(f"課-{COURSE_A}", "optional"),
                                                      (f"課-{COURSE_B}", "required")}
    assert row.copies == 3 and row.priority == "medium"


def test_charts_and_table_share_the_same_base(shared_book):
    d = dashboard._load_data([DEPT_A])

    assert sum(d["priority"].values()) == len(d["recs"]) == sum(d["status"].values()) == 1


def test_department_filter_shows_a_shared_book_with_whole_school_copies(shared_book):
    (in_a,) = _rows(dashboard._load_data([DEPT_A]))
    (in_b,) = _rows(dashboard._load_data([DEPT_B]))

    assert in_a.copies == in_b.copies == 3                     # 冊數是全校合計，不隨篩選變
    assert [c.in_scope for c in sorted(in_a.courses, key=lambda c: c.name)] == [True, False]
    assert [c.in_scope for c in sorted(in_b.courses, key=lambda c: c.name)] == [False, True]
    assert _rows(dashboard._load_data(["NO_SUCH_DEPT"])) == []


def test_unfiltered_view_marks_no_course_as_in_scope(shared_book):
    (row,) = _rows(dashboard._load_data())

    assert not any(c.in_scope for c in row.courses)


def test_rec_table_marks_every_course_category_and_multiple_btypes():
    row = dashboard.RecRow(
        priority="medium",
        courses=[dashboard.CourseRef("課甲", "optional", False), dashboard.CourseRef("課乙", "required", True)],
        title="T", status="owned_physical", copies=3, rationale="r",
    )

    html = dashboard._rec_table([row], [])

    assert _has(html, 'data-btypes="required optional"')       # 兩個類別都留得住：選「指定」或「參考」都會看到這列
    assert _has(html, "課甲（參考）") and _has(html, "課乙（指定）")
    assert _has(html, 'class="c-in"')                          # 屬於所選學系的課有標記


def test_report_rows_join_course_names(shared_book):
    (row,) = [r for r in main._fetch_report_rows() if r["title"] == TITLE]

    assert f"課-{COURSE_A}" in row["course_name"] and f"課-{COURSE_B}" in row["course_name"]
    assert row["is_required"] is True                          # 任一課指定
    assert (row["priority"], row["suggested_copies"]) == ("medium", 3)
    assert set(row) == {"priority", "course_id", "course_name", "title",
                        "is_required", "suggested_copies", "rationale"}     # CSV 的 DictWriter 依賴這組鍵


def test_export_recommendations_lists_every_course_of_the_book(shared_book):
    text = TestClient(dashboard.app).get("/export/recommendations").text

    rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if len(r) > 2 and r[2] == TITLE]
    assert len(rows) == 1
    assert f"課-{COURSE_A}" in rows[0][1] and f"課-{COURSE_B}" in rows[0][1]
    assert "指定" in rows[0][3] and "參考" in rows[0][3]
