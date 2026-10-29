"""recommender 的合併計算 merge_group 與逐課類別的理由 prompt（純函式，不碰 DB / 網路）。"""
from types import SimpleNamespace

from library_agent.agents import recommender as R
from library_agent.state import HoldingStatus, PurchasePriority

_next_citation = iter(range(1, 10_000))


def m(course_id, enrolled, *, required=True, holdings=0, status=HoldingStatus.MISSING,
      holding_id=None, name=None):
    cid = next(_next_citation)
    return R.Member(
        holding_id=holding_id if holding_id is not None else cid,
        holdings_count=holdings, status=status, course_id=course_id,
        course_name=name or f"課{course_id}", citation_id=cid,
        is_required=required, enrolled=enrolled,
    )


# ---------- merge_group ----------

def test_enrolment_is_summed_over_distinct_courses_once():
    # 同一門課列了同一本書兩次（8 組實例）：那門課的人數只能算一次
    d = R.merge_group([m("A", 50), m("A", 50), m("B", 25)])

    assert d.enrolled_total == 75
    assert d.copies == 3                      # ceil(75/25) = 3，沒有館藏


def test_holdings_are_deducted_once_not_per_member():
    # 三門課各 50 人（各需 2 本）、現有 3 本：逐列各扣一次會得到 0；合併後 6 − 3 = 3
    d = R.merge_group([m(c, 50, holdings=3, status=HoldingStatus.OWNED_PHYSICAL) for c in "ABC"])

    assert d.current_copies == 3
    assert d.copies == 3
    assert d.priority == PurchasePriority.MEDIUM


def test_any_required_course_makes_the_whole_book_required():
    # 館藏有但不足：指定 → MEDIUM，全是參考 → LOW
    owned = dict(holdings=2, status=HoldingStatus.OWNED_PHYSICAL)
    mixed = R.merge_group([m("A", 80, required=False, **owned), m("B", 20, required=True, **owned)])
    only_optional = R.merge_group([m("A", 80, required=False, **owned), m("B", 20, required=False, **owned)])

    assert mixed.is_required is True and mixed.priority == PurchasePriority.MEDIUM
    assert only_optional.is_required is False and only_optional.priority == PurchasePriority.LOW


def test_any_ebook_owned_skips_the_whole_book():
    d = R.merge_group([m("A", 100), m("B", 100, status=HoldingStatus.OWNED_EBOOK)])

    assert d.status == HoldingStatus.OWNED_EBOOK
    assert (d.priority, d.copies) == (PurchasePriority.SKIP, 0)


def test_representative_is_the_largest_holding_then_the_smallest_id():
    a = m("A", 10, holdings=1, holding_id=20)
    b = m("B", 10, holdings=3, holding_id=11)
    c = m("C", 10, holdings=3, holding_id=10)

    d = R.merge_group([a, b, c])

    assert d.representative.holding_id == 10
    assert d.current_copies == 3


def test_status_comes_from_the_representative():
    missing = m("A", 10, holdings=0, status=HoldingStatus.MISSING)
    owned = m("B", 10, holdings=2, status=HoldingStatus.OWNED_PHYSICAL)

    assert R.merge_group([missing, owned]).status == HoldingStatus.OWNED_PHYSICAL


def test_all_enrolments_unknown_defaults_to_one_copy():
    d = R.merge_group([m("A", 0), m("B", 0)])

    assert (d.priority, d.copies) == (PurchasePriority.HIGH, 1)


def test_cap_applies_to_the_merged_total():
    d = R.merge_group([m(str(i), 100) for i in range(10)])    # 1000 人、沒有館藏

    assert d.copies == R._settings.max_purchase_copies


def test_citation_ids_are_sorted_and_cover_every_member():
    members = [m("A", 10), m("B", 10), m("C", 10)]

    d = R.merge_group(list(reversed(members)))

    assert d.citation_ids == tuple(sorted(x.citation_id for x in members))


# ---------- 理由 prompt：逐課帶入各自的類別 ----------

def _capture_prompt(monkeypatch, reply="ok"):
    seen = {}

    def create(**kwargs):
        seen["messages"] = kwargs["messages"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    monkeypatch.setattr(R._client.chat.completions, "create", create)
    return seen


def _line(prompt, needle):
    return next(ln for ln in prompt.splitlines() if needle in ln)


def test_rationale_prompt_lists_each_course_with_its_own_category(monkeypatch):
    # 預覽時實測：只傳一個「是否指定」，LLM 會把「一門指定、一門參考」寫成「兩門都是指定用書」
    seen = _capture_prompt(monkeypatch)

    R._llm_rationale(
        "Intro", [("課甲", 80, False), ("課乙", 20, True)],
        HoldingStatus.OWNED_PHYSICAL, 100, 2, PurchasePriority.MEDIUM, 2,
    )

    prompt = seen["messages"][-1]["content"]
    assert "80" in _line(prompt, "課甲") and "參考" in _line(prompt, "課甲")
    assert "20" in _line(prompt, "課乙") and "指定" in _line(prompt, "課乙")
    assert "100" in prompt                                   # 合計人數


def test_rationale_marks_unknown_enrolment(monkeypatch):
    seen = _capture_prompt(monkeypatch)

    R._llm_rationale("Intro", [("課甲", 0, True)], HoldingStatus.MISSING, 0, 0,
                     PurchasePriority.HIGH, 1)

    assert "不明" in seen["messages"][-1]["content"]


def test_rationale_falls_back_to_template_when_llm_fails(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(R._client.chat.completions, "create", boom)

    text = R._llm_rationale(
        "Intro", [("課甲", 80, False), ("課乙", 20, True)],
        HoldingStatus.OWNED_PHYSICAL, 100, 2, PurchasePriority.MEDIUM, 2,
    )

    assert "100" in text and "建議採購 2 冊" in text and "2 門課" in text


def test_rationale_skip_fallback_is_unchanged(monkeypatch):
    monkeypatch.setattr(R._client.chat.completions, "create",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))

    text = R._llm_rationale("Intro", [("課甲", 80, True)], HoldingStatus.OWNED_PHYSICAL, 80, 5,
                            PurchasePriority.SKIP, 0)

    assert text == "館藏已足夠，暫不需採購。"
