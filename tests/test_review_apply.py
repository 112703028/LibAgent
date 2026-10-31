"""review_apply：把審核決定反映到館藏與採購建議（獨立測試資料庫；Alma 與 LLM 都以假的取代）。

流程：對「已核准、尚未套用」的書 → 有採購書名或沒有館藏者重查 Alma → recommender 重算全校 →
有出現在採購建議裡的才標 review_applied_at（失敗的不標，下次可重試）。
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from library_agent import dashboard
from library_agent import review_apply as RV
from library_agent.agents import librarian as L
from library_agent.agents import recommender as R
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)

FOUND3 = SimpleNamespace(found=True, has_ebook=False, physical_count=3, mms_id="999")
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(isolated_db, monkeypatch):
    """Alma 與 LLM 都換成假的；check_calls 記錄每次查 Alma 的 (isbn, title)。"""
    state = SimpleNamespace(Session=isolated_db, check_calls=[], alma=lambda isbn, title: FOUND3)

    def fake_check(isbn, title):
        state.check_calls.append((isbn, title))
        return state.alma(isbn, title)

    for mod in (R, L, RV, dashboard):
        monkeypatch.setattr(mod, "SessionLocal", isolated_db)
    monkeypatch.setattr(L, "check_holding", fake_check)
    monkeypatch.setattr(R, "_llm_rationale", lambda *a, **k: "RAT")
    monkeypatch.setattr(R, "_make_judge", lambda: (lambda a, b: False))
    monkeypatch.setattr(R, "fetch_student_number", lambda *a, **k: None)
    return state


def decided(S, n, *, review="approved", choice="matched", purchase_title=None, holding=None, applied=False):
    """播種第 n 門課的一個書目；holding=(狀態, 冊數) 時一併建館藏列。回傳 verified_book id。"""
    with S() as s:
        s.add(Course(course_id=f"P{n}", course_name=f"課-P{n}", semester="114-1", source_file="t",
                     raw_content="t", enrolled_count=100))
        s.flush()
        c = Citation(course_id=f"P{n}", title=f"書{n}", authors="[]", is_required=True, raw_mention="x", confidence=0.9)
        s.add(c)
        s.flush()
        vb = VerifiedBook(citation_id=c.id, canonical_title=f"配到的書{n}", canonical_authors="[]",
                          isbn_13="9780306406157", source="nla", verified=True, review_status=review,
                          review_choice=choice, purchase_title=purchase_title,
                          purchase_authors="[]" if purchase_title else None,
                          reviewed_at=NOW, review_applied_at=NOW if applied else None)
        s.add(vb)
        s.flush()
        if holding:
            s.add(HoldingCheck(verified_book_id=vb.id, status=holding[0], holdings_count=holding[1]))
        s.commit()
        return vb.id


def applied_at(S, vb_id):
    with S() as s:
        return s.get(VerifiedBook, vb_id).review_applied_at


def rec_citation_ids(S):
    with S() as s:
        return set(s.scalars(select(RecommendationCourse.citation_id)))


# ---------- 待套用清單 ----------

def test_pending_apply_ids_lists_only_approved_unapplied_books(world):
    want = decided(world.Session, 1)
    decided(world.Session, 2, applied=True)                       # 已套用
    decided(world.Session, 3, review="pending", choice=None)      # 還沒決定
    decided(world.Session, 4, review="rejected", choice="none")   # 退回不用套用
    decided(world.Session, 5, review="approved", choice=None)     # 舊的命令列核准（沒有決定紀錄）

    with world.Session() as s:
        assert RV.pending_apply_ids(s) == [want]


# ---------- 套用 ----------

def test_apply_rechecks_overridden_books_then_recomputes_and_marks_applied(world):
    vb = decided(world.Session, 1, choice="original", purchase_title="我要買的", holding=("missing", 0))

    result = RV.apply_review()

    assert world.check_calls == [(None, "我要買的")]                   # 用採購書名查，不是配到的書名／ISBN
    assert (result.applied, result.rechecked, result.errors) == (1, 1, [])
    with world.Session() as s:
        hc = s.scalar(select(HoldingCheck).where(HoldingCheck.verified_book_id == vb))
        assert (hc.status, hc.holdings_count) == ("owned_physical", 3)
        assert s.scalar(select(Recommendation.id)) is not None        # 出現在採購建議裡
    assert applied_at(world.Session, vb) is not None


def test_apply_for_a_matched_book_with_a_holding_only_recomputes(world):
    vb = decided(world.Session, 1, choice="matched", holding=("missing", 0))

    result = RV.apply_review()

    assert world.check_calls == [] and result.rechecked == 0          # 館藏本來就是查配到那本的，不重查
    assert result.applied == 1 and applied_at(world.Session, vb) is not None


def test_apply_rechecks_a_matched_book_that_has_no_holding_row(world):
    vb = decided(world.Session, 1, choice="matched", holding=None)

    result = RV.apply_review()

    assert len(world.check_calls) == 1 and result.rechecked == 1
    assert applied_at(world.Session, vb) is not None


def test_apply_does_nothing_when_nothing_is_pending(world, monkeypatch):
    monkeypatch.setattr(R, "recommender_node", lambda *a, **k: pytest.fail("沒有待套用時不該重算"))

    assert RV.apply_review() == RV.ApplyResult(0, 0, [])


# ---------- 失敗不留半套 ----------

def test_a_failed_recheck_leaves_the_book_unapplied_and_is_retried_next_time(world):
    vb = decided(world.Session, 1, choice="custom", purchase_title="我要買的", holding=("missing", 0))
    world.alma = lambda isbn, title: (_ for _ in ()).throw(RuntimeError("Alma 掛了"))

    first = RV.apply_review()

    assert first.applied == 0 and len(first.errors) == 1
    assert applied_at(world.Session, vb) is None                      # 沒標，下次可重試

    world.alma = lambda isbn, title: FOUND3
    second = RV.apply_review()

    assert second.applied == 1 and applied_at(world.Session, vb) is not None


def test_a_recommender_exception_marks_nothing_as_applied(world, monkeypatch):
    vb = decided(world.Session, 1, choice="matched", holding=("missing", 0))

    def boom(*args, **kwargs):
        raise RuntimeError("recommender 掛了")

    monkeypatch.setattr(R, "recommender_node", boom)

    with pytest.raises(RuntimeError):
        RV.apply_review()

    assert applied_at(world.Session, vb) is None


# ---------- 端點 ----------

@pytest.fixture
def client(world, monkeypatch):
    # 每個測試用全新的執行狀態，不污染別的測試；背景工作換成不做事的假的
    monkeypatch.setattr(dashboard, "_run_state", {"status": "idle", "started_at": None, "finished_at": None,
                                                  "error": None, "run_id": None})
    monkeypatch.setattr(dashboard, "_run_apply", lambda: None)
    return TestClient(dashboard.app)


def test_apply_endpoint_says_nothing_to_apply(client):
    body = client.post("/review/apply").json()

    assert body["ok"] is False and "沒有待套用" in body["message"]


def test_apply_endpoint_refuses_while_a_run_is_in_progress(client, world):
    decided(world.Session, 1, choice="matched", holding=("missing", 0))
    dashboard._run_state["status"] = "running"

    body = client.post("/review/apply").json()

    assert body["ok"] is False and "執行中" in body["message"]


def test_apply_endpoint_starts_the_job_and_marks_the_run_as_running(client, world):
    decided(world.Session, 1, choice="matched", holding=("missing", 0))

    body = client.post("/review/apply").json()

    assert body == {"ok": True, "count": 1}
    assert dashboard._run_state["status"] == "running"
