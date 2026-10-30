"""一本書一筆的讀取端：dashboard、報表、匯出。

播種方式同 tests/test_dashboard_filters.py：在開發庫播種專用資料、事後清掉（這裡只讀，不會改到真實資料）。
一本書（TESTBOOK-DD）被兩門課用到，分屬兩個學系：
  課甲 TESTDDA（學系 TESTDEPT_DDA，參考，60 人）、課乙 TESTDDB（學系 TESTDEPT_DDB，指定，40 人）
建議只有一筆，全校合計 3 本；關聯表記錄兩門課各自的類別與人數。
"""
import csv
import io
import re

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


COURSE_S = ["TESTDDS1", "TESTDDS2", "TESTDDS3"]      # 兩個班同名（S1、S2）+ 一門別的課（S3）
TITLE_S = "TESTBOOK-SECT"


def _cleanup():
    ids = [COURSE_A, COURSE_B, *COURSE_S]
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


@pytest.fixture
def sectioned_book():
    """一本書被三個班用到，其中兩個班同名（真實資料：大學英文（一）有 42 個班、經濟學 15 個班）。"""
    _cleanup()
    with SessionLocal() as s:
        parts = []
        for cid, name in zip(COURSE_S, ("B同名課", "B同名課", "A別的課")):
            s.add(Course(course_id=cid, course_name=name, semester="9999", source_file="t",
                         raw_content="t", department=DEPT_A, enrolled_count=30))
            s.flush()
            c = Citation(course_id=cid, title=TITLE_S, authors="[]", is_required=True,
                         raw_mention="指定", confidence=0.9)
            s.add(c)
            s.flush()
            vb = VerifiedBook(citation_id=c.id, canonical_title=TITLE_S, canonical_authors="[]",
                              isbn_13="9780199296095", source="nla", verified=True)
            s.add(vb)
            s.flush()
            hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
            s.add(hc)
            s.flush()
            parts.append((cid, c, hc))
        rec = Recommendation(course_id=parts[0][0], holding_id=parts[0][2].id, priority="high",
                             suggested_copies=3, rationale="r", book_key="test:sect")
        s.add(rec)
        s.flush()
        for cid, c, _ in parts:
            s.add(RecommendationCourse(recommendation_id=rec.id, course_id=cid, citation_id=c.id,
                                       is_required=True, enrolled=30))
        s.commit()
    yield
    _cleanup()


def test_sections_with_the_same_name_are_collapsed_with_a_count(sectioned_book):
    # 資料層記錄了三個班；畫面上同名的班合成一項，但必須標出班數，否則「這本書被 42 個班用到」完全看不出來
    (row,) = [r for r in dashboard._load_data()["recs"] if r.title == TITLE_S]

    assert [(c.name, c.sections) for c in row.courses] == [("A別的課", 1), ("B同名課", 2)]
    html = dashboard._rec_table([row], [])
    assert _has(html, "B同名課（指定）×2")
    assert not _has(html, "A別的課（指定）×")        # 只有一個班的不加 ×1


def test_a_filtered_view_marks_the_section_group_in_scope_when_any_section_matches(sectioned_book):
    (row,) = [r for r in dashboard._load_data([DEPT_A])["recs"] if r.title == TITLE_S]

    assert all(c.in_scope for c in row.courses)


# ---------- 單筆追溯：端點與展開鈕 ----------

def _rec_id():
    with SessionLocal() as s:
        return s.query(Recommendation.id).join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id) \
            .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id) \
            .filter(VerifiedBook.canonical_title == TITLE).scalar()


def test_trace_endpoint_returns_a_fragment_with_every_course(shared_book):
    resp = TestClient(dashboard.app).get(f"/trace/{_rec_id()}")

    assert resp.status_code == 200 and "text/html" in resp.headers["content-type"]
    assert all(_has(resp.text, f'data-sec="{n}"') for n in range(1, 6))
    assert _has(resp.text, f"課-{COURSE_A}") and _has(resp.text, f"課-{COURSE_B}")
    assert not _has(resp.text, "<html")                  # 是片段，不是整頁


def test_trace_endpoint_404_for_unknown_id():
    assert TestClient(dashboard.app).get("/trace/999999999").status_code == 404


def test_rec_table_rows_carry_rec_id_and_a_text_free_toggle():
    row = dashboard.RecRow("medium", [dashboard.CourseRef("課甲", "required", False)], "T",
                           "owned_physical", 3, "r", rec_id=42)

    html = dashboard._rec_table([row], [])

    assert _has(html, 'data-rec="42"')
    assert re.search(r'<button class="tbtn"[^>]*></button>', html)   # 按鈕內沒有文字：匯出 CSV 讀 textContent 不會被污染


def test_pending_rows_carry_vb_id_and_a_toggle():
    pending = [("課甲", "T", 0.5, "nla", True, "指定", 77)]

    html = dashboard._rec_table([], pending)

    assert _has(html, 'data-vb="77"') and not _has(html, "data-rec")
    assert re.search(r'<button class="tbtn"[^>]*></button>', html)
