"""recommend(only_citation_ids)：只重算「包含指定書目」的書（套用審核結果用），不動全校。

重點：
  - 只處理受影響的組；其他書的建議原封不動（即使它們的資料已變）
  - 同書認定改變（買原書讓這本書離開原來的 ISBN 組）時，舊建議要被重算或刪掉，不能留殘渣
  - 同書判斷（LLM）只問相關的書名，不重問無關的
  - 不做全校的清理（不再存在的建議、舊格式列）
"""
import json

from sqlalchemy import select

from library_agent.agents import recommender as R
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)
from tests.test_recommender_node import ISBN_A, ISBN_B, book, node, recs, seed   # noqa: F401  （node 是 fixture）

ISBN_C = "9780140449136"


def by_key(Session):
    return {r.book_key: r for r in recs(Session)}


def link_cits(Session, rec_id):
    with Session() as s:
        return sorted(s.scalars(select(RecommendationCourse.citation_id)
                                .where(RecommendationCourse.recommendation_id == rec_id)))


def override(Session, vb_id, title, isbn=None):
    with Session() as s:
        row = s.get(VerifiedBook, vb_id)
        row.review_status, row.review_choice = "approved", "original"
        row.purchase_title, row.purchase_authors, row.purchase_isbn = title, "[]", isbn
        s.commit()


def test_scoped_run_only_touches_the_requested_books(node):
    (ca, _va, ha), (_cb, _vb, _hb), (_cc, _vc, _hc) = seed(
        node.Session, book("C1", title="A", isbn=ISBN_A), book("C2", title="B", isbn=ISBN_B, enrolled=30),
        book("C3", title="C", isbn=ISBN_C))
    R.recommend()
    before = {k: (r.suggested_copies, r.rationale) for k, r in by_key(node.Session).items()}
    node.llm_calls.clear()
    with node.Session() as s:                 # B 的人數大增（全校重算會改 B），A 的館藏增加
        s.query(Course).filter(Course.course_id == "C2").update({"enrolled_count": 500})
        s.query(HoldingCheck).filter(HoldingCheck.id == ha).update({"holdings_count": 1, "status": "owned_physical"})
        s.commit()

    R.recommend(only_citation_ids={ca})

    after = by_key(node.Session)
    assert [c[0] for c in node.llm_calls] == ["A"]                                  # 只重寫 A 的理由
    assert after[f"isbn:{ISBN_B}"].suggested_copies == before[f"isbn:{ISBN_B}"][0]  # B 沒被動到
    assert after[f"isbn:{ISBN_A}"].suggested_copies != before[f"isbn:{ISBN_A}"][0]  # A 已更新
    R.recommend()                                                                   # 全校重算才會更新 B
    assert by_key(node.Session)[f"isbn:{ISBN_B}"].suggested_copies != before[f"isbn:{ISBN_B}"][0]


def test_scoped_run_recomputes_the_old_group_when_a_book_leaves_it(node):
    (c1, _v1, _h1), (c2, v2, _h2) = seed(
        node.Session, book("C1", title="Intro", isbn=ISBN_A, enrolled=30),
        book("C2", title="Intro", isbn=ISBN_A, enrolled=60))
    R.recommend()
    assert list(by_key(node.Session)) == [f"isbn:{ISBN_A}"]
    override(node.Session, v2, "Intro 原書", isbn=None)          # 買原書、原書沒有 ISBN：離開 ISBN 組

    R.recommend(only_citation_ids={c2})

    got = by_key(node.Session)
    assert len(got) == 2 and f"isbn:{ISBN_A}" in got
    old = got[f"isbn:{ISBN_A}"]
    assert link_cits(node.Session, old.id) == [c1]               # 舊組只剩 C1，已重算
    (new_key,) = [k for k in got if k != f"isbn:{ISBN_A}"]
    assert link_cits(node.Session, got[new_key].id) == [c2]


def test_scoped_run_deletes_the_old_recommendation_when_its_only_book_changes_identity(node):
    ((c1, v1, _h),) = seed(node.Session, book("C1", title="Intro", isbn=ISBN_A))
    R.recommend()
    override(node.Session, v1, "Other Title", isbn=None)

    R.recommend(only_citation_ids={c1})

    keys = list(by_key(node.Session))
    assert len(keys) == 1 and not keys[0].startswith("isbn:")      # 舊的 isbn 建議被刪，只剩新的


def test_scoped_run_does_not_ask_the_judge_about_unrelated_title_buckets(node, monkeypatch):
    asked = []
    monkeypatch.setattr(R, "_make_judge", lambda: (lambda a, b: asked.append(a["title"]) or False))
    (_c1, _v1, _h1), (_c2, _v2, _h2), (ct, _vt, _ht) = seed(
        node.Session,
        book("C1", title="Same Title", isbn=None, authors=["a"]), book("C2", title="Same Title", isbn=None, authors=["b"]),
        book("C3", title="Target", isbn=ISBN_A))
    R.recommend()
    assert asked == ["Same Title"]                       # 全校重算會問無關書名群的同書判斷
    asked.clear()

    R.recommend(only_citation_ids={ct})

    assert asked == []                                   # 限縮後不再重問


def test_scoped_run_does_not_clean_up_unrelated_stale_or_legacy_recommendations(node):
    (ca, _va, ha), (cb, _vb, _hb) = seed(node.Session, book("C1", title="A", isbn=ISBN_A), book("C2", title="B", isbn=ISBN_B))
    R.recommend()
    with node.Session() as s:
        s.add(Recommendation(course_id="C1", holding_id=ha, priority="low", suggested_copies=1,
                             rationale="stale", book_key="zzz-stale"))
        s.add(Recommendation(course_id="C1", holding_id=ha, priority="low", suggested_copies=1,
                             rationale="legacy", book_key=None))
        s.commit()

    R.recommend(only_citation_ids={cb})
    assert {"zzz-stale", None} <= {r.book_key for r in recs(node.Session)}      # 全校的清理留給全校重算

    R.recommend()
    assert {r.book_key for r in recs(node.Session)} == {f"isbn:{ISBN_A}", f"isbn:{ISBN_B}"}


def test_full_run_still_works_through_the_node_entry_point(node):
    seed(node.Session, book("C1", title="A", isbn=ISBN_A))

    R.recommender_node({})

    assert [r.book_key for r in recs(node.Session)] == [f"isbn:{ISBN_A}"]
