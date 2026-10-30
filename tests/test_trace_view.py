"""trace_view：一本書的決策過程（課綱 → 解析 → 配對 → 館藏 → 冊數怎麼算）。

資料用獨立測試資料庫（conftest 的 isolated_db）播種；算式說明另有不碰資料庫的純函式測試，
鎖住「畫面上的說明」與 recommender.merge_group 的實際結果一致。
"""
import json

import pytest
from sqlalchemy import select

from library_agent import trace_view as T
from library_agent.agents import recommender as R
from library_agent.book_kind import book_type_key, is_ai_recommended
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)
from library_agent.state import HoldingStatus

ISBN = "9780306406157"


def world(S, specs, *, stored=("medium", 3), links=True, key="isbn:1"):
    """播種一本書與它的建議。specs：每筆書目一個 dict。回傳 recommendation id。"""
    made = []
    with S() as s:
        for sp in specs:
            cid = sp["course_id"]
            course = s.scalar(select(Course).where(Course.course_id == cid))
            if course is None:
                course = Course(course_id=cid, course_name=sp.get("course_name", f"課-{cid}"),
                                semester="114-1", source_file="t",
                                raw_content=sp.get("syllabus", f"課綱：{cid}"),
                                enrolled_count=sp.get("enrolled", 30))
                s.add(course)
                s.flush()
            c = Citation(course_id=cid, title=sp.get("title", "Intro"),
                         authors=json.dumps(sp.get("authors", ["原作者"]), ensure_ascii=False),
                         edition=sp.get("edition"), isbn=sp.get("isbn"), publisher=sp.get("publisher"),
                         year=sp.get("year"), is_required=sp.get("required", True),
                         raw_mention=sp.get("mention", "指定 Intro"), confidence=0.9)
            s.add(c)
            s.flush()
            vb = VerifiedBook(citation_id=c.id, canonical_title=sp.get("matched_title", "Intro"),
                              canonical_authors=json.dumps(sp.get("matched_authors", ["原作者"]), ensure_ascii=False),
                              isbn_13=ISBN, source="nla", verified=True,
                              pairing_verdict=sp.get("verdict"), pairing_judge=sp.get("judge"),
                              pairing_reason=sp.get("reason"), review_status=sp.get("review"))
            s.add(vb)
            s.flush()
            hc = None
            if sp.get("holding", True):
                hc = HoldingCheck(verified_book_id=vb.id, status=sp.get("status", "owned_physical"),
                                  holdings_count=sp.get("holdings", 1), alma_mms_id=sp.get("mms", "991"),
                                  matched_edition=sp.get("hedition"))
                s.add(hc)
                s.flush()
            made.append((cid, c, hc, sp.get("enrolled", 30)))
        rep = next(m for m in made if m[2] is not None)
        rec = Recommendation(course_id=rep[0], holding_id=rep[2].id, priority=stored[0],
                             suggested_copies=stored[1], rationale="RAT", book_key=key)
        s.add(rec)
        s.flush()
        if links:
            for cid, c, _hc, enrolled in made:
                s.add(RecommendationCourse(recommendation_id=rec.id, course_id=cid, citation_id=c.id,
                                           is_required=c.is_required, enrolled=enrolled))
        s.commit()
        return rec.id


def trace_of(S, rec_id):
    with S() as s:
        return T.build_trace(s, rec_id)


TWO = [dict(course_id="C1", enrolled=60, required=False, mention="參考 Intro"),
       dict(course_id="C2", enrolled=40, required=True)]


# ---------- book_kind ----------

def test_book_kind_rules():
    assert book_type_key(True, "指定") == "required"
    assert book_type_key(False, None) == "optional"
    assert book_type_key(True, "[AI推薦] 理由") == "ai" and is_ai_recommended("[AI推薦] x")


# ---------- build_trace ----------

def test_build_trace_collects_every_source_of_the_book(isolated_db):
    trace = trace_of(isolated_db, world(isolated_db, TWO))

    assert sorted((s.course_id, s.kind, s.enrolled) for s in trace.sources) == [
        ("C1", "optional", 60), ("C2", "required", 40)]
    assert sorted(x.course_id for x in trace.syllabi) == ["C1", "C2"]
    assert trace.book_key == "isbn:1" and trace.rationale == "RAT"


def test_syllabus_is_per_course_and_truncated(isolated_db):
    specs = [dict(course_id="C1", syllabus="字" * 20000), dict(course_id="C1", title="Intro 2nd")]
    trace = trace_of(isolated_db, world(isolated_db, specs))

    (syllabus,) = trace.syllabi                      # 同一門課兩個書目只有一份課綱
    assert len(syllabus.text) == T.MAX_SYLLABUS and syllabus.length == 20000
    assert len(trace.sources) == 2


def test_identical_pairings_are_grouped_and_different_ones_listed(isolated_db):
    same = dict(verdict="same", judge="rule", reason="書名相同")
    specs = [dict(course_id="C1", **same), dict(course_id="C2", **same),
             dict(course_id="C3", matched_title="別本書", verdict="mismatch", judge="llm", reason="作者不同")]
    trace = trace_of(isolated_db, world(isolated_db, specs))

    assert sorted(len(p.originals) for p in trace.pairings) == [1, 2]
    bad = [p for p in trace.pairings if p.verdict == "mismatch"]
    assert len(bad) == 1 and bad[0].reason == "作者不同" and bad[0].judge == "llm"


def test_legacy_recommendation_without_links_falls_back_to_its_own_book(isolated_db):
    trace = trace_of(isolated_db, world(isolated_db, [dict(course_id="C1")], links=False, key=None))

    assert [s.course_id for s in trace.sources] == ["C1"] and trace.calc is not None


def test_unknown_id_returns_none(isolated_db):
    assert trace_of(isolated_db, 999999) is None


def test_member_without_a_holding_is_skipped_from_the_calculation(isolated_db):
    specs = [dict(course_id="C1", enrolled=60), dict(course_id="C2", enrolled=40, holding=False)]
    trace = trace_of(isolated_db, world(isolated_db, specs))

    assert len(trace.sources) == 2                    # 來源與配對仍列出
    assert trace.calc.enrolled_total == 60            # 沒有館藏資料的成員不納入算式


def test_render_copes_with_a_book_that_has_no_holding_data():
    # 沒有任何館藏資料時（holding / calc 為 None）畫面要寫得出來，不能丟例外
    trace = T.Trace(rec_id=1, title="t", book_key=None, rationale=None,
                    syllabi=[], sources=[], pairings=[], holding=None, calc=None)

    html = T.render_trace(trace)

    assert "尚無館藏" in html and 'data-sec="5"' in html


# ---------- 算式說明必須與 merge_group 一致 ----------

def member(course, enrolled, *, required=True, holdings=0, status=HoldingStatus.MISSING, hid=None):
    i = hid or abs(hash((course, enrolled, holdings))) % 10_000
    return R.Member(i, holdings, status, course, f"課{course}", i, required, enrolled)


CASES = [
    [member("A", 50, holdings=3, status=HoldingStatus.OWNED_PHYSICAL, hid=1),
     member("B", 50, holdings=3, status=HoldingStatus.OWNED_PHYSICAL, hid=2),
     member("C", 50, holdings=3, status=HoldingStatus.OWNED_PHYSICAL, hid=3)],
    [member("A", 0, hid=4), member("B", 0, hid=5)],
    [member("A", 100, hid=6), member("B", 100, status=HoldingStatus.OWNED_EBOOK, hid=7)],
    [member(str(i), 100, hid=10 + i) for i in range(10)],
    [member("A", 50, hid=30), member("A", 50, hid=31), member("B", 25, hid=32)],
    [member("A", 40, required=False, holdings=2, status=HoldingStatus.OWNED_PHYSICAL, hid=40)],
]


@pytest.mark.parametrize("members", CASES)
def test_calc_explanation_agrees_with_merge_group(members):
    decision = R.merge_group(members)

    calc = T._calc(members, decision.priority.value, decision.copies)

    assert (calc.priority, calc.copies) == (decision.priority.value, decision.copies)
    assert calc.enrolled_total == decision.enrolled_total
    if decision.status != HoldingStatus.OWNED_EBOOK and decision.copies:
        assert min(calc.gap, calc.cap) == calc.copies        # 畫面寫的「缺口 → 套上限」確實等於最後的冊數
    assert calc.consistent is True


def test_calc_flags_drift_when_stored_numbers_differ(isolated_db):
    consistent = trace_of(isolated_db, world(isolated_db, TWO, stored=("medium", 3)))
    assert consistent.calc.consistent is True
    assert "tr-drift" not in T.render_trace(consistent)


def test_drift_is_reported_when_the_stored_recommendation_differs(isolated_db):
    trace = trace_of(isolated_db, world(isolated_db, TWO, stored=("low", 99)))

    assert trace.calc.consistent is False and trace.calc.stored_copies == 99
    html = T.render_trace(trace)
    assert "tr-drift" in html and "99" in html


# ---------- render_trace ----------

def test_render_escapes_untrusted_text(isolated_db):
    evil = "<script>alert(1)</script>"
    specs = [dict(course_id="C1", syllabus=evil, mention=evil, title='"><img src=x onerror=1>',
                  authors=[evil], matched_title=evil, matched_authors=[evil], reason=evil)]
    html = T.render_trace(trace_of(isolated_db, world(isolated_db, specs)))

    assert evil not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_render_has_five_sections_in_order(isolated_db):
    html = T.render_trace(trace_of(isolated_db, world(isolated_db, TWO)))

    positions = [html.index(f'data-sec="{n}"') for n in range(1, 6)]
    assert positions == sorted(positions)


def test_render_marks_a_mismatch_with_its_reason(isolated_db):
    specs = [dict(course_id="C1", verdict="mismatch", judge="llm", reason="書名相同但作者不同", review="pending")]
    html = T.render_trace(trace_of(isolated_db, world(isolated_db, specs)))

    assert "tr-bad" in html and "書名相同但作者不同" in html and "配錯" in html and "待審" in html


def test_render_shows_the_arithmetic(isolated_db):
    html = T.render_trace(trace_of(isolated_db, world(isolated_db, TWO)))

    assert "100" in html and "25" in html          # 合計 100 人、每 25 人 1 本
    assert "3" in html and "medium" in html
