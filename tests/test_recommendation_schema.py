"""recommendations.book_key 與 recommendation_courses 的資料庫層保證。

這些都是「少了會讓別的節點崩潰」的約束：validator.py / librarian.py 會直接 DELETE recommendations，
parser.py 會先刪舊 citations；沒有 ON DELETE CASCADE，新表的外鍵會擋住它們。
沿用 tests/test_graph_routing.py 的作法：用真實 DB 播種專用資料，finally 清理。
"""
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)
from library_agent.db.session import SessionLocal

COURSE = "TESTSCH"


def _cleanup():
    with SessionLocal() as s:
        cit_ids = [i for (i,) in s.query(Citation.id).filter(Citation.course_id == COURSE)]
        vb_ids = [i for (i,) in s.query(VerifiedBook.id).filter(VerifiedBook.citation_id.in_(cit_ids))]
        hc_ids = [i for (i,) in s.query(HoldingCheck.id).filter(HoldingCheck.verified_book_id.in_(vb_ids))]
        s.execute(delete(Recommendation).where(Recommendation.course_id == COURSE))
        s.query(HoldingCheck).filter(HoldingCheck.id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(VerifiedBook).filter(VerifiedBook.id.in_(vb_ids)).delete(synchronize_session=False)
        s.query(Citation).filter(Citation.course_id == COURSE).delete(synchronize_session=False)
        s.query(Course).filter(Course.course_id == COURSE).delete(synchronize_session=False)
        s.commit()


@pytest.fixture
def seeded():
    """一門課、兩本書（各有書目、已驗證書、館藏），回傳 [(citation_id, holding_id), ...]。"""
    _cleanup()
    out = []
    with SessionLocal() as s:
        s.add(Course(course_id=COURSE, course_name="測試課", semester="9999",
                     source_file="test", raw_content="test"))
        s.flush()
        for n in (1, 2):
            c = Citation(course_id=COURSE, title=f"TESTBOOK-{n}", authors="[]", is_required=True,
                         raw_mention="指定", confidence=0.9)
            s.add(c)
            s.flush()
            vb = VerifiedBook(citation_id=c.id, canonical_title=f"TESTBOOK-{n}", canonical_authors="[]",
                              source="nla", verified=True)
            s.add(vb)
            s.flush()
            hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
            s.add(hc)
            s.flush()
            out.append((c.id, hc.id))
        s.commit()
    yield out
    _cleanup()


def _rec(holding_id, book_key=None):
    return Recommendation(course_id=COURSE, holding_id=holding_id, priority="high",
                          suggested_copies=1, rationale="r", book_key=book_key)


def _link_count(rec_id=None):
    with SessionLocal() as s:
        q = select(func.count()).select_from(RecommendationCourse)
        if rec_id is not None:
            q = q.where(RecommendationCourse.recommendation_id == rec_id)
        return s.scalar(q)


def test_book_key_is_unique_but_null_is_allowed(seeded):
    (_, h1), (_, h2) = seeded
    with SessionLocal() as s:        # 兩筆舊格式（NULL）可以並存：重建前舊資料不會互相衝突
        s.add_all([_rec(h1), _rec(h2)])
        s.commit()

    with pytest.raises(IntegrityError):
        with SessionLocal() as s:    # 兩筆同一個非空 book_key：一本書只能有一筆
            s.add_all([_rec(h1, "isbn:9780306406157"), _rec(h2, "isbn:9780306406157")])
            s.commit()


def test_deleting_a_recommendation_removes_its_course_rows(seeded):
    # 模擬 validator / librarian 的直接 DELETE recommendations
    (c1, h1), _ = seeded
    with SessionLocal() as s:
        rec = _rec(h1, "isbn:1")
        s.add(rec)
        s.flush()
        s.add(RecommendationCourse(recommendation_id=rec.id, course_id=COURSE, citation_id=c1,
                                   is_required=True, enrolled=30))
        s.commit()
        rec_id = rec.id
    assert _link_count(rec_id) == 1

    with SessionLocal() as s:
        s.execute(delete(Recommendation).where(Recommendation.id == rec_id))
        s.commit()

    assert _link_count(rec_id) == 0


def test_deleting_a_citation_removes_its_course_rows(seeded):
    # 模擬 parser 先刪舊書目。citation 若同時被已驗證書引用會先被那邊擋住，
    # 所以用一個沒有已驗證書的書目，單獨驗證關聯表這條外鍵的 cascade。
    (_, h1), _ = seeded
    with SessionLocal() as s:
        lone = Citation(course_id=COURSE, title="LONE", authors="[]", is_required=False,
                        raw_mention="參考", confidence=0.9)
        s.add(lone)
        s.flush()
        rec = _rec(h1, "isbn:2")
        s.add(rec)
        s.flush()
        s.add(RecommendationCourse(recommendation_id=rec.id, course_id=COURSE, citation_id=lone.id,
                                   is_required=False, enrolled=10))
        s.commit()
        rec_id, lone_id = rec.id, lone.id
    assert _link_count(rec_id) == 1

    with SessionLocal() as s:
        s.execute(delete(Citation).where(Citation.id == lone_id))
        s.commit()

    assert _link_count(rec_id) == 0


def test_same_citation_cannot_be_linked_twice_to_one_recommendation(seeded):
    (c1, h1), _ = seeded
    with SessionLocal() as s:
        rec = _rec(h1, "isbn:3")
        s.add(rec)
        s.flush()
        s.add(RecommendationCourse(recommendation_id=rec.id, course_id=COURSE, citation_id=c1,
                                   is_required=True, enrolled=1))
        s.commit()
        rec_id = rec.id

    with pytest.raises(IntegrityError):
        with SessionLocal() as s:
            s.add(RecommendationCourse(recommendation_id=rec_id, course_id=COURSE, citation_id=c1,
                                       is_required=True, enrolled=1))
            s.commit()
