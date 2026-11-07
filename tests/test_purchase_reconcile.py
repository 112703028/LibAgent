"""重算採購建議後的購物車對帳：書的識別鍵改變時，決定依課綱書目 id 搬到新鍵（獨立測試資料庫）。

規則：鍵還在 → 只更新快照；鍵不在 → 決定記下的課綱書目中仍屬於某筆建議者全部指向同一本、且那本還沒有決定 → 搬過去；
其他情況（新書已有決定、拆成兩本、書不見了、沒有快照）一律不動，成為「找不到的決定」。
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from library_agent.agents import recommender as R
from library_agent.db.models import PurchaseDecision, Recommendation, VerifiedBook
from library_agent.purchase_reconcile import reconcile, snapshot
from tests.test_recommender_node import ISBN_A, ISBN_B, book, node, seed   # noqa: F401  （node 是 fixture）

T0 = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def keys(S):
    with S() as s:
        return sorted(s.scalars(select(Recommendation.book_key)).all())


def rec_of(S, key):
    with S() as s:
        return s.scalar(select(Recommendation).where(Recommendation.book_key == key))


def decide(S, key, copies=3, *, at=T0, snap=True, title=None, cids=None):
    """直接插一筆決定。snap=True 時用 snapshot() 取目前的快照（和端點寫入的一樣）。"""
    with S() as s:
        rec = s.scalar(select(Recommendation).where(Recommendation.book_key == key))
        if snap:
            title, cids = snapshot(s, key)
        s.add(PurchaseDecision(book_key=key, copies=copies, suggested_at_decision=rec.suggested_copies if rec else 1,
                               decided_at=at, title=title, citation_ids=None if cids is None else json.dumps(cids)))
        s.commit()


def decisions(S):
    with S() as s:
        return {d.book_key: d for d in s.scalars(select(PurchaseDecision))}


def set_isbn(S, vb_id, isbn):
    with S() as s:
        s.get(VerifiedBook, vb_id).isbn_13 = isbn
        s.commit()


def test_unchanged_key_only_refreshes_the_snapshot(node):
    ((cit, _vb, _hc),) = seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommender_node({})
    (key,) = keys(node.Session)
    decide(node.Session, key, snap=False, title=None, cids=[])          # 快照是空的（例如升級前的舊決定）

    with node.Session() as s:
        result = reconcile(s)
        s.commit()

    d = decisions(node.Session)[key]
    assert result.refreshed == 1 and result.moved == 0 and result.orphaned == 0
    assert d.title == "Intro" and json.loads(d.citation_ids) == [cit]


def test_changed_key_moves_the_decision_and_keeps_copies_and_the_suggestion_then(node):
    ((_cit, vb, _hc),) = seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommender_node({})
    (old_key,) = keys(node.Session)
    decide(node.Session, old_key, copies=3)
    suggested_then = decisions(node.Session)[old_key].suggested_at_decision

    set_isbn(node.Session, vb, ISBN_A)                                    # 模擬書商補上 ISBN
    R.recommender_node({})

    (new_key,) = keys(node.Session)
    assert new_key == f"isbn:{ISBN_A}" and new_key != old_key
    d = decisions(node.Session)
    assert list(d) == [new_key]
    assert d[new_key].copies == 3 and d[new_key].suggested_at_decision == suggested_then
    assert d[new_key].decided_at == T0


def test_target_that_already_has_a_decision_is_not_overwritten(node):
    ((_c, vb_x, _h),) = seed(node.Session, book("C1", title="Intro", isbn=None))
    seed(node.Session, book("C2", title="Other", isbn=ISBN_A))
    R.recommender_node({})
    x_key = next(k for k in keys(node.Session) if k.startswith("title:"))
    decide(node.Session, x_key, copies=3)
    decide(node.Session, f"isbn:{ISBN_A}", copies=7)

    set_isbn(node.Session, vb_x, ISBN_A)                                  # X 併進已有決定的那本
    R.recommender_node({})

    d = decisions(node.Session)
    assert d[f"isbn:{ISBN_A}"].copies == 7                                # 新書原本的決定不被覆蓋
    assert x_key in d and d[x_key].copies == 3                           # 舊的留著（成為找不到的決定）


def test_two_old_decisions_merging_into_one_book_move_only_the_newer_one(node):
    (_c1, vb1, _h1), (_c2, vb2, _h2) = seed(node.Session, book("C1", title="Intro", isbn=None),
                                            book("C2", title="Other", isbn=None))
    R.recommender_node({})
    k1, k2 = keys(node.Session)
    older, newer = (k1, k2)
    decide(node.Session, older, copies=2, at=T0)
    decide(node.Session, newer, copies=5, at=T0 + timedelta(hours=1))

    set_isbn(node.Session, vb1, ISBN_A)
    set_isbn(node.Session, vb2, ISBN_A)                                   # 兩本被認定為同一本
    R.recommender_node({})

    d = decisions(node.Session)
    assert d[f"isbn:{ISBN_A}"].copies == 5                                # 較新的那筆搬過去
    assert older in d and d[older].copies == 2                           # 另一筆不覆蓋、不撞唯一鍵


def test_book_split_into_two_is_left_as_an_orphan(node):
    (_c1, vb1, _h1), (_c2, vb2, _h2) = seed(node.Session, book("C1", title="Intro", isbn=None),
                                            book("C2", title="Intro", isbn=None))
    R.recommender_node({})
    (key,) = keys(node.Session)                                           # 同名、資料相同 → 一本書
    decide(node.Session, key, copies=4)

    set_isbn(node.Session, vb1, ISBN_A)
    set_isbn(node.Session, vb2, ISBN_B)                                   # 不同 ISBN → 拆成兩本
    R.recommender_node({})

    d = decisions(node.Session)
    assert list(d) == [key] and d[key].copies == 4                       # 不猜要搬去哪一本
    assert len(keys(node.Session)) == 2


def test_book_that_left_the_list_is_left_as_an_orphan(node):
    ((_cit, vb, _hc),) = seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommender_node({})
    (key,) = keys(node.Session)
    decide(node.Session, key, copies=3)
    with node.Session() as s:
        s.get(VerifiedBook, vb).review_status = "rejected"                # 審核退回：不再可採購
        s.commit()

    R.recommender_node({})

    assert keys(node.Session) == []
    assert decisions(node.Session)[key].copies == 3


def test_decision_without_citation_ids_is_left_alone(node):
    seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommender_node({})
    decide(node.Session, "title:gone", copies=2, snap=False, title=None, cids=None)

    with node.Session() as s:
        result = reconcile(s)
        s.commit()

    assert result.orphaned == 1 and decisions(node.Session)["title:gone"].copies == 2


def test_scoped_recommend_also_reconciles(node):
    ((cit, _vb, _hc),) = seed(node.Session, book("C1", title="Intro", isbn=None))
    R.recommender_node({})
    (key,) = keys(node.Session)
    decide(node.Session, key, snap=False, title=None, cids=[])

    R.recommend(only_citation_ids={cit})

    d = decisions(node.Session)[key]
    assert d.title == "Intro" and json.loads(d.citation_ids) == [cit]
