"""review_groups：把待審書目分類並合併成「一組一本書」（純函式，不碰資料庫）。"""
from library_agent import review_groups as G

TH = 0.6


def item(vb_id, *, title="Macroeconomics", matched="Macroeconomics", course="C1", enrolled=30,
         verified=True, verdict="mismatch", judge="llm", reason="作者不同", confidence=0.9, cit=None,
         authors=("原作者",), matched_authors=("別人",), source="google_books"):
    return G.PendingItem(
        vb_id=vb_id, citation_id=cit if cit is not None else vb_id, course_id=course,
        course_name=f"課-{course}", enrolled=enrolled, title=title, authors=list(authors),
        matched_title=matched, matched_authors=list(matched_authors), source=source, verified=verified,
        verdict=verdict, judge=judge, reason=reason, confidence=confidence,
    )


def notfound(vb_id, **kw):
    kw.setdefault("verified", False)
    kw.setdefault("verdict", None)
    kw.setdefault("judge", None)
    kw.setdefault("reason", None)
    kw.setdefault("source", "ncl_not_found")
    return item(vb_id, **kw)


def other(vb_id, **kw):
    kw.setdefault("verdict", None)
    kw.setdefault("judge", None)
    kw.setdefault("reason", None)
    return item(vb_id, **kw)


def run(items, rec_of=None):
    return G.group_pending(items, rec_of or {}, TH)


# ---------- 分類 ----------

def test_classify_mismatch_wins_over_notfound():
    assert G.classify(item(1, verdict="mismatch", verified=False)) == "mismatch"
    assert G.classify(notfound(2)) == "notfound"
    assert G.classify(other(3)) == "other"


# ---------- 分組 ----------

def test_mismatch_with_same_title_but_different_matched_books_stay_separate():
    items = [item(1, matched="Macroeconomics: A Modern Approach", course="C1"),
             item(2, matched="Macroeconomics", course="C2")]

    assert len(run(items)["mismatch"]) == 2        # 同名不同作者的書配到不同本，絕不能併成一組


def test_mismatch_with_same_original_and_matched_merge_into_one_group():
    items = [item(1, course="C1", enrolled=60), item(2, course="C2", enrolled=40)]

    (group,) = run(items)["mismatch"]

    assert group.items == 2 and group.vb_ids == (1, 2)
    assert [c[0] for c in group.courses] == ["課-C1", "課-C2"] and group.enrolled_total == 100


def test_notfound_groups_by_original_title_only():
    items = [notfound(1, course="C1", matched="x"), notfound(2, course="C2", matched="y")]

    (group,) = run(items)["notfound"]

    assert group.items == 2                         # 查無此書的 matched_title 只是占位，不參與分組


def test_other_items_never_merge():
    assert len(run([other(1), other(2)])["other"]) == 2


def test_enrolment_counts_each_course_once():
    items = [item(1, course="C1", enrolled=60), item(2, course="C1", enrolled=60)]   # 同課列了同一本書兩次

    (group,) = run(items)["mismatch"]

    assert group.courses == [("課-C1", 60)] and group.enrolled_total == 60 and group.items == 2


def test_title_normalization_merges_case_and_width_variants():
    items = [notfound(1, title="Intro to Politics", course="C1"), notfound(2, title="INTRO TO POLITICS", course="C2")]

    assert len(run(items)["notfound"]) == 1


def test_empty_input_gives_three_empty_lists():
    assert G.group_pending([], {}, TH) == {"mismatch": [], "notfound": [], "other": []}


def test_low_confidence_flag_is_true_when_any_member_is_below_threshold():
    items = [notfound(1, course="C1", confidence=0.9), notfound(2, course="C2", confidence=0.3)]

    (group,) = run(items)["notfound"]

    assert group.low_confidence is True
    assert run([notfound(3, confidence=0.9)])["notfound"][0].low_confidence is False


# ---------- 採購清單狀況 ----------

def test_notfound_group_carries_the_most_severe_priority_of_its_members():
    items = [notfound(1, course="C1"), notfound(2, course="C2")]

    (group,) = run(items, {1: ("low", 2), 2: ("high", 4)})["notfound"]

    assert (group.priority, group.copies) == ("high", 4)


def test_group_not_in_any_recommendation_has_no_priority():
    (group,) = run([item(1)])["mismatch"]

    assert group.priority is None and group.copies is None


# ---------- 排序 ----------

def test_notfound_sorted_by_priority_then_enrolment():
    items = [notfound(1, title="A", enrolled=10), notfound(2, title="B", enrolled=5),
             notfound(3, title="C", enrolled=100), notfound(4, title="D", enrolled=50)]
    rec_of = {1: ("medium", 1), 2: ("high", 1), 4: ("medium", 1)}        # 3 不在任何建議裡

    order = [g.title for g in run(items, rec_of)["notfound"]]

    assert order == ["B", "D", "A", "C"]


def test_mismatch_sorted_by_enrolment_desc():
    items = [item(1, title="A", matched="a2", enrolled=10), item(2, title="B", matched="b2", enrolled=90),
             item(3, title="C", matched="c2", enrolled=40)]

    assert [g.title for g in run(items)["mismatch"]] == ["B", "C", "A"]
