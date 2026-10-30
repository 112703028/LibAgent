"""recommender_node：一本書一筆建議（跨課程合併）。

recommender 永遠以全校資料計算並清掉不再存在的建議，所以這些測試一律在獨立的測試資料庫
（conftest 的 isolated_db）裡跑，絕不碰開發資料庫。LLM 與判斷函式以假的取代並計次。
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from library_agent.agents import recommender as R
from library_agent.agents.match_auditor import _norm
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)

ISBN_A = "9780306406157"
ISBN_B = "9780199296095"
ALMA_ID = "991010871719705721"      # 不是有效 ISBN


@pytest.fixture
def node(isolated_db, monkeypatch):
    llm_calls = []

    def fake_rationale(*args, **kwargs):
        llm_calls.append(args)
        return "RAT"

    judge_answer = {"same": False}
    monkeypatch.setattr(R, "SessionLocal", isolated_db)
    monkeypatch.setattr(R, "_llm_rationale", fake_rationale)
    monkeypatch.setattr(R, "_make_judge", lambda: (lambda a, b: judge_answer["same"]))
    monkeypatch.setattr(R, "fetch_student_number", lambda *a, **k: None)   # 不打網路
    return SimpleNamespace(Session=isolated_db, llm_calls=llm_calls, judge=judge_answer)


def _course(session, course_id):
    # Course 的主鍵是整數 id；course_id 是另一個唯一欄位，不能用 session.get 查
    return session.scalar(select(Course).where(Course.course_id == course_id))


def seed(Session, *books):
    """books：dict(course_id, enrolled, title, isbn, authors, required, holdings, status, verdict)。
    回傳每本的 (citation_id, verified_book_id, holding_id)。"""
    out = []
    with Session() as s:
        for b in books:
            if _course(s, b["course_id"]) is None:
                s.add(Course(course_id=b["course_id"], course_name=f"課-{b['course_id']}", semester="9999",
                             source_file="t", raw_content="t", enrolled_count=b.get("enrolled", 30)))
                s.flush()
            c = Citation(course_id=b["course_id"], title=b["title"], authors="[]",
                         is_required=b.get("required", True), raw_mention="x", confidence=0.9)
            s.add(c)
            s.flush()
            vb = VerifiedBook(citation_id=c.id, canonical_title=b["title"],
                              canonical_authors=json.dumps(b.get("authors", []), ensure_ascii=False),
                              isbn_13=b.get("isbn"), source="nla", verified=True,
                              pairing_verdict=b.get("verdict"))
            s.add(vb)
            s.flush()
            hc = HoldingCheck(verified_book_id=vb.id, status=b.get("status", "missing"),
                              holdings_count=b.get("holdings", 0))
            s.add(hc)
            s.flush()
            out.append((c.id, vb.id, hc.id))
        s.commit()
    return out


def recs(Session):
    with Session() as s:
        return s.execute(select(Recommendation).order_by(Recommendation.id)).scalars().all()


def links(Session, rec_id):
    with Session() as s:
        return s.execute(select(RecommendationCourse).where(
            RecommendationCourse.recommendation_id == rec_id)).scalars().all()


def book(course, title="Intro", isbn=ISBN_A, **kw):
    return dict(course_id=course, title=title, isbn=isbn, **kw)


def test_same_isbn_in_two_courses_becomes_one_recommendation(node):
    seed(node.Session,
         book("C1", enrolled=60, required=False, holdings=1, status="owned_physical"),
         book("C2", enrolled=40, required=True, holdings=1, status="owned_physical"))

    R.recommender_node({})

    (rec,) = recs(node.Session)
    assert rec.book_key == f"isbn:{ISBN_A}"
    assert rec.suggested_copies == 3                       # 合計 100 人 → 4 本，現有 1 本只扣一次
    assert rec.priority == "medium"                        # 館藏不足 + 任一課指定
    assert sorted((l.course_id, l.is_required, l.enrolled) for l in links(node.Session, rec.id)) == [
        ("C1", False, 60), ("C2", True, 40)]
    assert len(node.llm_calls) == 1                        # 一本書只寫一次理由


def test_the_rationale_gets_each_course_with_its_own_category(node):
    seed(node.Session,
         book("C1", enrolled=80, required=False),
         book("C2", enrolled=20, required=True))

    R.recommender_node({})

    course_lines = node.llm_calls[0][1]
    assert sorted(course_lines) == [("課-C1", 80, False), ("課-C2", 20, True)]


def test_same_course_listing_a_book_twice_counts_its_enrolment_once(node):
    seed(node.Session,
         book("C1", enrolled=50), book("C1", enrolled=50, title="Intro (2nd printing)"),
         book("C2", enrolled=25))

    R.recommender_node({})

    (rec,) = recs(node.Session)
    assert rec.suggested_copies == 3                       # (50 + 25) / 25，而不是 (50+50+25) / 25 = 5
    assert len(links(node.Session, rec.id)) == 3           # 三個書目都記錄，人數只算兩門課


def test_priority_follows_the_strictest_course(node):
    seed(node.Session, book("C1", enrolled=30, required=False), book("C2", enrolled=30, required=True))

    R.recommender_node({})

    assert recs(node.Session)[0].priority == "high"        # 館藏不存在 + 任一課指定 → HIGH（全參考只會是 MEDIUM）


def test_representative_columns_point_at_the_largest_holding(node):
    ids = seed(node.Session,
               book("C1", enrolled=30, holdings=1, status="owned_physical"),
               book("C2", enrolled=30, holdings=3, status="owned_physical"))

    R.recommender_node({})

    (rec,) = recs(node.Session)
    assert rec.holding_id == ids[1][2] and rec.course_id == "C2"


def test_rerun_without_changes_calls_no_llm_and_keeps_the_id(node):
    seed(node.Session, book("C1", enrolled=60), book("C2", enrolled=40))
    R.recommender_node({})
    first = recs(node.Session)[0]

    R.recommender_node({})

    assert len(node.llm_calls) == 1                        # 簽章沒變：不重打 LLM
    (again,) = recs(node.Session)
    assert again.id == first.id
    assert len(links(node.Session, again.id)) == 2


def test_changed_enrolment_updates_in_place_and_rewrites_the_rationale(node):
    seed(node.Session, book("C1", enrolled=60), book("C2", enrolled=40))
    R.recommender_node({})
    first = recs(node.Session)[0]
    with node.Session() as s:
        _course(s, "C2").enrolled_count = 140               # 合計 200 人 → 8 本
        s.commit()

    R.recommender_node({})

    (rec,) = recs(node.Session)
    assert rec.id == first.id                              # 就地更新，不是刪了重建
    assert rec.suggested_copies == 5                       # 8 本被上限 5 截斷
    assert len(node.llm_calls) == 2
    assert sorted(l.enrolled for l in links(node.Session, rec.id)) == [60, 140]


def test_stale_and_legacy_recommendations_are_removed(node):
    ids = seed(node.Session, book("C1", title="Keep", isbn=ISBN_A), book("C2", title="Drop", isbn=ISBN_B))
    R.recommender_node({})
    with node.Session() as s:
        s.get(VerifiedBook, ids[1][1]).pairing_verdict = "mismatch"      # 這本變成不可採購
        s.add(Recommendation(course_id="C1", holding_id=ids[0][2], priority="low",
                             suggested_copies=1, rationale="舊格式", book_key=None))
        s.commit()

    R.recommender_node({})

    keys = [r.book_key for r in recs(node.Session)]
    assert keys == [f"isbn:{ISBN_A}"]                      # 不可採購的書與舊格式（book_key 為 NULL）都被清掉


def test_scoped_run_still_uses_whole_school_totals_and_keeps_other_courses(node):
    seed(node.Session,
         book("C1", enrolled=60), book("C2", enrolled=40),
         book("C3", title="Other", isbn=ISBN_B, enrolled=25))
    R.recommender_node({})
    before = {r.book_key: r.id for r in recs(node.Session)}

    R.recommender_node({"course_ids": ["C1"]})             # 只跑某一門課：合計仍須是全校，別的建議也不能被刪

    after = {r.book_key: r.id for r in recs(node.Session)}
    assert after == before
    assert len(links(node.Session, before[f"isbn:{ISBN_A}"])) == 2


def test_books_with_the_same_invalid_isbn_but_different_authors_stay_separate(node):
    node.judge["same"] = False
    seed(node.Session,
         book("C1", title="經濟學", isbn=ALMA_ID, authors=["許嘉棟"]),
         book("C2", title="經濟學", isbn=ALMA_ID, authors=["毛慶生"]))

    R.recommender_node({})

    norm = _norm("經濟學")                                   # 鍵用正規化書名（繁簡折疊），不寫死字形
    assert sorted(r.book_key for r in recs(node.Session)) == [f"title:{norm}", f"title:{norm}#2"]


def test_books_without_a_valid_isbn_are_merged_when_judged_the_same(node):
    node.judge["same"] = True
    seed(node.Session,
         book("C1", title="Economics", isbn=None, authors=["Parkin, Michael"]),
         book("C2", title="Economics", isbn=None, authors=["Michael Parkin"]))

    R.recommender_node({})

    assert [r.book_key for r in recs(node.Session)] == ["title:economics"]


def test_blocked_books_are_excluded_from_the_group(node):
    ids = seed(node.Session,
               book("C1", enrolled=60),
               book("C2", enrolled=40, verdict="mismatch"))           # 配錯且未放行

    R.recommender_node({})

    (rec,) = recs(node.Session)
    assert [l.citation_id for l in links(node.Session, rec.id)] == [ids[0][0]]
    assert rec.suggested_copies == 3                       # 只算 C1 的 60 人
