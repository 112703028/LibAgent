"""trace_view：一本書的決策過程（課綱 → 解析 → 配對 → 館藏 → 冊數怎麼算）。

資料用獨立測試資料庫（conftest 的 isolated_db）播種；算式說明另有不碰資料庫的純函式測試，
鎖住「畫面上的說明」與 recommender.merge_group 的實際結果一致。
"""
import json
from datetime import datetime, timezone

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


# ---------- 待審核的書（沒有採購建議）----------

def pending_world(S, *, verdict="mismatch", judge="llm", reason="書名相同但作者不同", review="pending",
                  confidence=0.9, holding=True, title="Original Title", matched_title="Other Book",
                  syllabus="課綱：P1", verified=True, source="nla", course_id="P1",
                  choice=None, purchase_title=None, applied=False):
    """播種一筆待審核的書（沒有 Recommendation），回傳 verified_book id。
    choice／purchase_title／applied：模擬已經在儀表板做過決定的書（審核紀錄）。"""
    with S() as s:
        course = s.scalar(select(Course).where(Course.course_id == course_id))
        if course is None:
            course = Course(course_id=course_id, course_name=f"課-{course_id}", semester="114-1",
                            source_file="t", raw_content=syllabus, enrolled_count=30)
            s.add(course)
            s.flush()
        c = Citation(course_id=course_id, title=title, authors=json.dumps(["原作者"], ensure_ascii=False),
                     is_required=True, raw_mention="指定 " + title, confidence=confidence)
        s.add(c)
        s.flush()
        vb = VerifiedBook(citation_id=c.id, canonical_title=matched_title,
                          canonical_authors=json.dumps(["別人"], ensure_ascii=False), isbn_13=ISBN,
                          source=source, verified=verified, pairing_verdict=verdict, pairing_judge=judge,
                          pairing_reason=reason, review_status=review, review_choice=choice,
                          purchase_title=purchase_title,
                          purchase_authors="[]" if purchase_title else None,
                          review_applied_at=datetime(2026, 11, 1, tzinfo=timezone.utc) if applied else None)
        s.add(vb)
        s.flush()
        if holding:
            s.add(HoldingCheck(verified_book_id=vb.id, status="owned_physical", holdings_count=2,
                               alma_mms_id="991"))
        s.commit()
        return vb.id


def pending_trace_of(S, vb_ids):
    ids = vb_ids if isinstance(vb_ids, list) else [vb_ids]
    with S() as s:
        return T.build_pending_trace(s, ids)


def test_pending_trace_of_a_mismatch_explains_why_it_is_waiting(isolated_db):
    trace = pending_trace_of(isolated_db, pending_world(isolated_db))

    assert trace.title == "Original Title"                     # 標題是原書名，不是配到的那本
    assert trace.calc is None and trace.holding is not None
    assert len(trace.sources) == 1 and len(trace.syllabi) == 1 and len(trace.pairings) == 1
    assert trace.pairings[0].verdict == "mismatch"
    assert "配錯" in trace.pending_reason and "書名相同但作者不同" in trace.pending_reason


def test_pending_trace_of_an_unaudited_book_blames_low_parse_confidence(isolated_db):
    vb = pending_world(isolated_db, verdict=None, judge=None, reason=None, confidence=0.30)

    trace = pending_trace_of(isolated_db, vb)

    assert trace.pairings[0].verdict is None
    assert "0.30" in trace.pending_reason and "尚未稽核" in trace.pending_reason


def test_pending_trace_copes_with_no_holding_row(isolated_db):
    trace = pending_trace_of(isolated_db, pending_world(isolated_db, holding=False))

    assert trace.holding is None and trace.pending_reason


@pytest.mark.parametrize("review", [None, "approved", "rejected"])
def test_pending_trace_is_none_unless_the_book_is_pending(isolated_db, review):
    assert pending_trace_of(isolated_db, pending_world(isolated_db, review=review)) is None


def test_pending_trace_unknown_id_returns_none(isolated_db):
    assert pending_trace_of(isolated_db, 999999) is None


def test_render_pending_says_it_is_not_in_the_purchase_list(isolated_db):
    html = T.render_trace(pending_trace_of(isolated_db, pending_world(isolated_db)))

    assert all(f'data-sec="{n}"' in html for n in range(1, 6))
    assert "未進入採購建議" in html and "書名相同但作者不同" in html
    assert "配到的書" in html and "不代表原書" in html          # 館藏是配到那本的，要提醒
    assert "tr-bad" in html                                     # 配錯的配對標紅
    assert "<h3 class=\"tr-title\">Original Title</h3>" in html


def test_render_pending_escapes_untrusted_text(isolated_db):
    evil = "<script>alert(1)</script>"
    vb = pending_world(isolated_db, reason=evil, title=evil, matched_title=evil, syllabus=evil)

    html = T.render_trace(pending_trace_of(isolated_db, vb))

    assert evil not in html and "&lt;script&gt;" in html


def test_pending_trace_of_a_book_the_validator_could_not_find(isolated_db):
    vb = pending_world(isolated_db, verdict=None, judge=None, reason=None, verified=False,
                       source="ncl_not_found", matched_title="Original Title")

    trace = pending_trace_of(isolated_db, vb)

    assert trace.pairings[0].verified is False
    assert "查無此書" in trace.pending_reason and "ncl_not_found" in trace.pending_reason
    assert "尚未稽核" not in trace.pending_reason          # 沒配到書，談不上稽核


def test_render_unverified_pending_says_no_record_was_matched(isolated_db):
    vb = pending_world(isolated_db, verdict=None, judge=None, reason=None, verified=False,
                       source="ncl_not_found", matched_title="Original Title")

    html = T.render_trace(pending_trace_of(isolated_db, vb))

    assert "查無記錄" in html                                # 右側不能把原書名當成「配到的書」回顯
    assert "用原書名" in html and "不代表原書" not in html   # 館藏警語換成：是用原書名直接查的


def test_render_pending_reason_has_no_doubled_full_stop(isolated_db):
    html = T.render_trace(pending_trace_of(isolated_db, pending_world(isolated_db, reason="作者不同。")))

    assert "。。" not in html


# ---------- 代表館藏：第 ④ 段與第 ⑤ 段必須用同一筆 ----------

def test_holding_section_and_calculation_agree_when_a_non_representative_holding_grew(isolated_db):
    # recommender 當初選 C1 的館藏（2 冊）為代表；之後 C2 那筆被 librarian 更新成 3 冊，建議沒被刪
    # （librarian 只刪「指向被更新那筆」的建議）。第 ④ 段與第 ⑤ 段的「現有冊數」不能各說各話。
    specs = [dict(course_id="C1", enrolled=60, holdings=2), dict(course_id="C2", enrolled=40, holdings=3)]

    trace = trace_of(isolated_db, world(isolated_db, specs))

    assert trace.calc.current == 3
    assert trace.holding.copies == trace.calc.current


def _hc(id, copies):
    from types import SimpleNamespace
    return SimpleNamespace(id=id, holdings_count=copies)


def test_most_copies_wins_and_a_tie_goes_to_the_smaller_id():
    key = T._copies_then_smaller_id

    assert max([_hc(1, 2), _hc(2, 9), _hc(3, 5)], key=key).id == 2
    assert max([_hc(7, 3), _hc(4, 3)], key=key).id == 4


def test_null_copies_count_as_zero():
    assert max([_hc(1, None), _hc(2, 1)], key=T._copies_then_smaller_id).id == 2


# ---------- 待審核追溯：一組可含多個書目 ----------

def test_pending_trace_merges_several_items_of_one_group(isolated_db):
    v1 = pending_world(isolated_db, course_id="P1")
    v2 = pending_world(isolated_db, course_id="P2")

    trace = pending_trace_of(isolated_db, [v1, v2])

    assert len(trace.syllabi) == 2 and len(trace.sources) == 2
    assert len(trace.pairings) == 1 and len(trace.pairings[0].originals) == 2     # 相同的配對合併
    assert trace.title == "Original Title"


def test_pending_trace_skips_items_no_longer_pending(isolated_db):
    v1 = pending_world(isolated_db, course_id="P1")
    v2 = pending_world(isolated_db, course_id="P2", review="approved")     # 已被別人放行

    trace = pending_trace_of(isolated_db, [v1, v2])

    assert len(trace.sources) == 1 and trace.sources[0].course_id == "P1"


def test_pending_trace_with_no_ids_returns_none(isolated_db):
    assert pending_trace_of(isolated_db, []) is None


def _client(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient
    from library_agent import dashboard

    monkeypatch.setattr(dashboard, "SessionLocal", isolated_db)
    return TestClient(dashboard.app)


def test_pending_endpoint_accepts_several_ids(isolated_db, monkeypatch):
    client = _client(isolated_db, monkeypatch)
    v1 = pending_world(isolated_db, course_id="P1")
    v2 = pending_world(isolated_db, course_id="P2")

    ok = client.get(f"/trace/pending?ids={v1}&ids={v2}")

    assert ok.status_code == 200 and "未進入採購建議" in ok.text and "<html" not in ok.text
    assert "課-P1" in ok.text and "課-P2" in ok.text


def test_pending_endpoint_rejects_missing_too_many_or_malformed_ids(isolated_db, monkeypatch):
    client = _client(isolated_db, monkeypatch)

    assert client.get("/trace/pending").status_code == 400
    assert client.get("/trace/pending?" + "&".join(f"ids={i}" for i in range(1, 52))).status_code == 400
    assert client.get("/trace/pending?ids=abc").status_code == 422        # 非數字：FastAPI 的預設回應


def test_pending_endpoint_404_when_nothing_is_pending(isolated_db, monkeypatch):
    client = _client(isolated_db, monkeypatch)
    v = pending_world(isolated_db, review="approved")

    assert client.get(f"/trace/pending?ids={v}").status_code == 404
    assert client.get("/trace/pending?ids=999999").status_code == 404


# ---------- 審核紀錄：已決定的書也能展開看決策過程 ----------

DECIDED = [("matched", "approved", "買配對到的書"), ("original", "approved", "買原書"),
           ("custom", "approved", "改買其他書名"), ("none", "rejected", "都不買")]


@pytest.mark.parametrize("choice,review,label", DECIDED)
def test_decided_books_can_be_traced(isolated_db, choice, review, label):
    pt = "我要買的書" if choice in ("original", "custom") else None
    vb = pending_world(isolated_db, review=review, choice=choice, purchase_title=pt)

    trace = pending_trace_of(isolated_db, vb)

    assert trace is not None and trace.pending_reason is None             # 已不是待審
    assert label in trace.decision and (pt is None or pt in trace.decision)


def test_the_section_five_of_a_decided_book_shows_the_decision_not_the_pending_note(isolated_db):
    waiting = pending_world(isolated_db, course_id="P1", review="approved", choice="original", purchase_title="X", applied=False)
    done = pending_world(isolated_db, course_id="P2", review="approved", choice="matched", applied=True)

    html_waiting = T.render_trace(pending_trace_of(isolated_db, waiting))
    html_done = T.render_trace(pending_trace_of(isolated_db, done))

    assert "人工決定" in html_waiting and "待套用" in html_waiting and "未進入採購建議" not in html_waiting
    assert "人工決定" in html_done and "已套用" in html_done and "未進入採購建議" not in html_done


def test_a_rejected_decision_says_it_will_not_be_purchased(isolated_db):
    vb = pending_world(isolated_db, review="rejected", choice="none")

    html = T.render_trace(pending_trace_of(isolated_db, vb))

    assert "都不買" in html and "不會採購" in html


def test_decision_text_escapes_the_purchase_title(isolated_db):
    evil = "<script>alert(1)</script>"
    vb = pending_world(isolated_db, review="approved", choice="custom", purchase_title=evil)

    html = T.render_trace(pending_trace_of(isolated_db, vb))

    assert evil not in html and "&lt;script&gt;" in html


def test_a_group_with_pending_and_decided_items_shows_both_notes(isolated_db):
    p = pending_world(isolated_db, course_id="P1")
    d = pending_world(isolated_db, course_id="P2", review="approved", choice="matched", applied=True)

    html = T.render_trace(pending_trace_of(isolated_db, [p, d]))

    assert "未進入採購建議" in html and "人工決定" in html


def test_endpoint_accepts_decided_ids(isolated_db, monkeypatch):
    client = _client(isolated_db, monkeypatch)
    v = pending_world(isolated_db, review="approved", choice="matched")

    resp = client.get(f"/trace/pending?ids={v}")

    assert resp.status_code == 200 and "人工決定" in resp.text
