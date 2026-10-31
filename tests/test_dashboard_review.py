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
COURSES = ["TESTRVM1", "TESTRVM2", "TESTRVN1", "TESTRVO1", "TESTRVP1", "TESTRVQ1"]
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
               source="nla", recommend=None, purchase_title=None, choice=None):
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
                      review_status=review, review_choice=choice, purchase_title=purchase_title,
                      purchase_authors="[]" if purchase_title else None)
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


# ---------- 渲染：審核卡片（不碰資料庫）----------

import re

from library_agent.review_groups import KINDS, ReviewGroup


def grp(kind, title="TitleX", *, matched="MatchedY", authors=("原作者",), matched_authors=("別人",),
        source="google_books", judge="llm", reason="作者不同", low=False, courses=(("課甲", 60),), items=1,
        priority=None, copies=None, vb_ids=(1,)):
    return ReviewGroup(kind=kind, title=title, authors=list(authors), matched_title=matched,
                       matched_authors=list(matched_authors), source=source, judge=judge, reason=reason,
                       low_confidence=low, courses=list(courses), items=items,
                       enrolled_total=sum(n for _, n in courses), vb_ids=tuple(vb_ids),
                       priority=priority, copies=copies)


def panel(**kw):
    return dashboard._review_panel({k: kw.get(k, []) for k in KINDS})


def test_review_panel_has_three_tabs_with_group_and_item_counts():
    html = panel(mismatch=[grp("mismatch", items=2, vb_ids=(1, 2)), grp("mismatch", "T2", vb_ids=(3,))],
                 notfound=[grp("notfound", "T3", vb_ids=(4,))])

    assert len(re.findall(r'class="rtab', html)) == 3
    assert "配錯" in html and "2 組（3 筆書目）" in html
    assert "查無此書" in html and "1 組（1 筆書目）" in html


def test_review_panel_lists_mismatch_with_both_books_and_reason():
    html = panel(mismatch=[grp("mismatch", "Macroeconomics", matched="Macroeconomics: A Modern Approach",
                               reason="書名相同但作者不同", judge="llm")])

    assert "Macroeconomics: A Modern Approach" in html and "書名相同但作者不同" in html
    assert "LLM" in html and "已擋下" in html            # 配錯的書不在採購清單
    assert 'data-ids="1"' in html


def test_review_panel_notfound_shows_no_record_and_the_priority_it_still_has():
    html = panel(notfound=[grp("notfound", source="ncl_not_found", judge=None, reason=None,
                               priority="high", copies=4)])

    assert "查無記錄" in html and "ncl_not_found" in html
    assert dashboard._PRIORITY_LABEL["high"] in html and "4" in html


def test_review_panel_empty_states():
    assert "目前沒有待審書目" in panel() and 'class="rtab' not in panel()
    one_empty = panel(mismatch=[grp("mismatch")])
    assert "目前沒有待審的書" in one_empty                 # 其他分頁沒有組：顯示說明、不報錯


def test_review_panel_opens_on_the_tab_with_the_most_groups():
    html = panel(mismatch=[grp("mismatch")],
                 notfound=[grp("notfound", "A", vb_ids=(2,)), grp("notfound", "B", vb_ids=(3,))])

    assert re.search(r'class="rtab active" data-k="notfound"', html)
    assert re.search(r'class="rpanel" data-k="mismatch" hidden', html)


def test_review_panel_escapes_untrusted_text():
    evil = "<script>alert(1)</script>"
    html = panel(mismatch=[grp("mismatch", evil, matched=evil, reason=evil, authors=(evil,), courses=((evil, 3),))])

    assert evil not in html and "&lt;script&gt;" in html


def test_review_panel_toggle_button_has_no_text():
    html = panel(mismatch=[grp("mismatch")])

    assert re.search(r'<button class="tbtn"[^>]*></button>', html)


# ---------- 渲染：採購表與整頁 ----------

def _row(**kw):
    return dashboard.RecRow("high", [dashboard.CourseRef("課甲", "required", False)], "T", "missing", 2, "r",
                            rec_id=5, **kw)


def test_unverified_recommendation_carries_a_text_free_marker():
    html = dashboard._rec_table([_row(unverified=True)])

    assert 'data-unverified="1"' in html
    assert re.search(r'<span class="unv"[^>]*></span>', html)          # 標記文字由 CSS 畫，匯出 CSV 才不會多出文字


def test_verified_recommendation_has_no_marker():
    html = dashboard._rec_table([_row()])

    assert "unv" not in html and "data-unverified" not in html


def test_rec_table_has_no_pending_rows():
    assert 'data-priority="pending"' not in dashboard._rec_table([_row()])


def _page(review=None):
    data = {"kpis": {"courses": 1, "citations": 1, "verified": 1, "pending": 3, "copies": 2},
            "priority": {"high": 1}, "status": {"missing": 1}, "recs": [_row()], "pending": [],
            "review": review or {k: [] for k in KINDS}}
    return dashboard._render(data)


def test_pending_is_no_longer_a_priority_filter_and_has_no_separate_link():
    html = _page()

    assert 'data-f="pending"' not in html                # 待審列不在採購表裡，不能再當篩選
    assert 'class="flink"' not in html                   # 審核分頁就在同一張卡片頂端，不需要「見下方」的連結
    assert html.count('id="review"') == 1


def _page_with_reviews():
    review = {k: [] for k in KINDS}
    review["mismatch"] = [grp("mismatch"), grp("mismatch", "T2", vb_ids=(2,))]
    review["notfound"] = [grp("notfound", "T3", vb_ids=(3,), source="ncl_not_found", judge=None, reason=None)]
    data = {"kpis": {"courses": 1, "citations": 1, "verified": 1, "pending": 3, "copies": 2},
            "priority": {"high": 1}, "status": {"missing": 1}, "recs": [_row()], "pending": [],
            "review": review, "decided": [], "pending_apply": 0}
    return dashboard._render(data)


def test_purchase_list_and_review_are_tabs_of_one_card():
    html = _page_with_reviews()

    assert html.count('id="review"') == 1 and html.count('id="rectable"') == 1
    card = html[html.index('id="review"'):]
    assert card.index('id="rectable"') > 0                               # 採購表在同一張卡片裡
    tabs = re.findall(r'class="rtab[^"]*" data-k="(\w+)"', html)
    assert tabs == ["recs", "mismatch", "notfound", "other"]             # 採購建議在最前面，沒有決定就沒有「已決定」


def test_the_purchase_tab_is_open_by_default_and_review_panels_are_hidden():
    html = _page_with_reviews()

    assert re.search(r'class="rtab active" data-k="recs"', html)
    assert re.search(r'class="rpanel" data-k="recs"', html)              # 採購建議面板不隱藏
    assert re.search(r'class="rpanel" data-k="mismatch" hidden', html)
    assert re.search(r'class="rpanel" data-k="notfound" hidden', html)


def test_the_purchase_tab_shows_how_many_books_there_are():
    assert "採購建議 1 本" in _page_with_reviews()


def test_kpi_pending_link_opens_the_biggest_review_tab():
    html = _page_with_reviews()

    assert 'data-review-open="mismatch"' in html                         # 配錯有 2 組，最多


def test_without_any_review_items_there_are_no_tab_bar_just_the_purchase_list():
    html = _page()

    assert 'class="rtab' not in html and 'id="rectable"' in html and 'id="review"' in html


def test_export_panel_has_no_pending_checkbox_and_kpi_links_to_the_review_panel():
    html = _page()

    assert 'class="expchk" value="pending"' not in html
    assert 'class="kpi" href="#review"' in html


# ---------- 採購書名與「未驗證」----------

@pytest.fixture
def decided_world():
    _cleanup()
    with SessionLocal() as s:
        # 買原書：驗證器配到「TESTBOOK-RV-BUY-MATCHED」，人工決定採購書名「TESTBOOK-RV-BUY」
        _seed_book(s, "TESTRVP1", DEPT_A, 20, "TESTBOOK-RV-BUY-MATCHED", verdict="mismatch", review="approved",
                   choice="original", purchase_title="TESTBOOK-RV-BUY", recommend=("high", 2))
        # 查無此書、但人工已確認（輸入書名加入）：不該再標「未驗證」
        _seed_book(s, "TESTRVQ1", DEPT_A, 20, "TESTBOOK-RV-CONFIRMED", verified=False, source="ncl_not_found",
                   review="approved", choice="custom", purchase_title="TESTBOOK-RV-CONFIRMED",
                   recommend=("high", 2))
        s.commit()
    yield
    _cleanup()


def test_recrow_shows_the_purchase_title(decided_world):
    titles = {r.title for r in dashboard._load_data()["recs"] if r.title.startswith("TESTBOOK-RV")}

    assert "TESTBOOK-RV-BUY" in titles and "TESTBOOK-RV-BUY-MATCHED" not in titles


def test_unverified_marker_disappears_once_a_human_approved_the_book(decided_world):
    recs = {r.title: r for r in dashboard._load_data()["recs"] if r.title.startswith("TESTBOOK-RV")}

    assert recs["TESTBOOK-RV-CONFIRMED"].unverified is False


def test_load_data_puts_decided_books_in_decided_not_in_pending_groups(decided_world):
    data = dashboard._load_data()

    decided = [g for g in data["decided"] if g.title.startswith("TESTBOOK-RV")]
    assert {(g.title, g.choice) for g in decided} == {("TESTBOOK-RV-BUY-MATCHED", "original"),
                                                      ("TESTBOOK-RV-CONFIRMED", "custom")}
    assert all(g.buy_title and not g.applied for g in decided)
    pending_titles = {g.title for groups in data["review"].values() for g in groups}
    assert not any(t.startswith("TESTBOOK-RV-BUY") or t.startswith("TESTBOOK-RV-CONF") for t in pending_titles)


def test_pending_apply_counts_approved_unapplied_books(decided_world):
    assert dashboard._load_data()["pending_apply"] >= 2          # 兩筆播種的決定（真實資料庫目前沒有任何決定）


# ---------- 審核卡片的動作區 ----------

import dataclasses


def decided_grp(**kw):
    base = dict(choice="original", buy_title="買的書", applied=False)
    base.update(kw)
    return dataclasses.replace(grp("mismatch", "原書X", vb_ids=(7,)), **base)


def test_group_pending_carries_choice_buy_title_and_applied():
    from library_agent.review_groups import PendingItem, group_pending

    def item(vb_id, course, applied):
        return PendingItem(vb_id=vb_id, citation_id=vb_id, course_id=course, course_name=course, enrolled=10,
                           title="T", authors=[], matched_title="M", matched_authors=[], source="nla",
                           verified=True, verdict="mismatch", judge="llm", reason="r", confidence=0.9,
                           choice="original", buy_title="T", applied=applied)

    groups = group_pending([item(1, "A", True), item(2, "B", False)], {}, 0.6)["mismatch"]

    assert [(g.choice, g.buy_title, g.applied) for g in groups] == [("original", "T", False)]   # 組內全部已套用才算已套用


def test_mismatch_row_has_three_action_buttons():
    html = panel(mismatch=[grp("mismatch")])

    assert all(f'data-act="{a}"' in html for a in ("original", "matched", "none"))
    assert 'data-act="custom"' not in html
    assert "買原書" in html and "買配對到的書" in html and "都不買" in html


def test_notfound_row_has_a_prefilled_title_box_an_isbn_box_and_two_buttons():
    html = panel(notfound=[grp("notfound", "找不到的書", source="ncl_not_found", judge=None, reason=None)])

    assert re.search(r'<input class="atitle"[^>]*value="找不到的書"', html)
    assert 'class="aisbn"' in html
    assert 'data-act="custom"' in html and 'data-act="none"' in html
    assert 'data-act="original"' not in html and 'data-act="matched"' not in html
    assert "加入採購清單" in html and "捨棄" in html


def test_other_row_has_two_buttons():
    html = panel(other=[grp("other", judge=None, reason=None)])

    assert 'data-act="matched"' in html and 'data-act="none"' in html
    assert "買這本書" in html and 'data-act="original"' not in html


def test_pending_rows_have_a_checkbox_for_batch_actions():
    html = panel(mismatch=[grp("mismatch")])

    assert 'class="rchk"' in html


def test_batch_bars_offer_only_matched_and_none():
    html = panel(mismatch=[grp("mismatch")], notfound=[grp("notfound", "B", vb_ids=(2,))],
                 other=[grp("other", "C", vb_ids=(3,))])

    assert 'data-batch="original"' not in html and 'data-batch="custom"' not in html
    chunks = html.split('<div class="rpanel"')[1:]          # 逐個面板切開，避免正規表示式跨面板
    panels = {m.group(1): chunk for chunk in chunks for m in [re.match(r' data-k="(\w+)"', chunk)] if m}
    mm, nf = panels["mismatch"], panels["notfound"]
    assert 'data-batch="matched"' in mm and 'data-batch="none"' in mm
    assert 'data-batch="none"' in nf and 'data-batch="matched"' not in nf     # 查無此書沒有「買配對到的書」


def test_decided_tab_lists_choice_status_and_undo():
    review = {k: [] for k in KINDS}
    done = [decided_grp(), decided_grp(choice="none", buy_title=None, applied=False, vb_ids=(8,)),
            decided_grp(choice="custom", buy_title="輸入的書", applied=True, vb_ids=(9,))]

    html = dashboard._review_panel(review, decided=done, pending_apply=0)

    assert 'data-k="decided"' in html and "已決定" in html
    assert "買原書" in html and "輸入書名加入" in html and "都不買" in html
    assert "待套用" in html and "已套用" in html
    assert html.count('data-undo') == 3
    decided_panel = re.search(r'<div class="rpanel" data-k="decided".*', html, re.S).group(0)
    assert 'class="tbtn"' not in decided_panel               # 已不是待審，沒有追溯可展開


def test_no_decided_tab_when_nothing_was_decided():
    assert 'data-k="decided"' not in panel(mismatch=[grp("mismatch")])


def test_apply_banner_shows_only_when_there_is_something_to_apply():
    review = {k: [] for k in KINDS}
    review["mismatch"] = [grp("mismatch")]

    with_banner = dashboard._review_panel(review, pending_apply=3)
    without = dashboard._review_panel(review, pending_apply=0)

    assert 'id="applybtn"' in with_banner and "3 筆決定待套用" in with_banner
    assert 'id="applybtn"' not in without


def test_action_area_escapes_untrusted_text():
    evil = '"><script>alert(1)</script>'
    html = panel(notfound=[grp("notfound", evil, source="ncl_not_found", judge=None, reason=None)])

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
