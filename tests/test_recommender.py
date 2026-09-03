from types import SimpleNamespace

from library_agent.agents import recommender as R
from library_agent.state import HoldingStatus, PurchasePriority


class _FakeSession:
    def __init__(self):
        self.updates = 0

    def execute(self, *a, **k):
        self.updates += 1


def _course(cid: str, enrolled: int):
    return SimpleNamespace(course_id=cid, semester="114-1", enrolled_count=enrolled)


def test_resolve_enrolled_fetches_once_and_persists(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(cid, sem):
        calls["n"] += 1
        return "42"

    monkeypatch.setattr(R, "fetch_student_number", fake_fetch)

    fs, cache, course = _FakeSession(), {}, _course("C1", 0)
    assert R._resolve_enrolled(fs, course, cache) == 42
    assert R._resolve_enrolled(fs, course, cache) == 42   # 同課第二次
    assert calls["n"] == 1        # memo：只打一次 HTTP
    assert fs.updates == 1        # 寫回 enrolled_count 一次


def test_resolve_enrolled_uses_db_value_without_http(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(cid, sem):
        calls["n"] += 1
        return "99"

    monkeypatch.setattr(R, "fetch_student_number", fake_fetch)

    fs = _FakeSession()
    assert R._resolve_enrolled(fs, _course("C2", 55), {}) == 55
    assert calls["n"] == 0        # DB 已有 → 不打 HTTP
    assert fs.updates == 0        # 不需寫回


# _decide：確定性優先級 + 冊數（預設 students_per_copy=25, max_purchase_copies=5）

def test_decide_ebook_is_skip():
    assert R._decide(HoldingStatus.OWNED_EBOOK, True, 200, 0) == (PurchasePriority.SKIP, 0)


def test_decide_missing_required_high_and_caps_copies():
    # 200/25=8, 缺口 8 → 封頂 5；完全沒藏 + 指定 → HIGH
    assert R._decide(HoldingStatus.MISSING, True, 200, 0) == (PurchasePriority.HIGH, 5)


def test_decide_missing_optional_is_medium():
    assert R._decide(HoldingStatus.MISSING, False, 200, 0) == (PurchasePriority.MEDIUM, 5)


def test_decide_owned_but_insufficient_required_is_medium_not_high():
    # 有藏(1)但不足 → MEDIUM（不是 HIGH），缺口 7 封頂 5
    assert R._decide(HoldingStatus.OWNED_PHYSICAL, True, 200, 1) == (PurchasePriority.MEDIUM, 5)


def test_decide_owned_enough_is_skip():
    # 50/25=2 需求，已有 3 → 缺口 <=0 → SKIP
    assert R._decide(HoldingStatus.OWNED_PHYSICAL, True, 50, 3) == (PurchasePriority.SKIP, 0)


def test_decide_unknown_enrollment_defaults_to_one():
    assert R._decide(HoldingStatus.MISSING, True, 0, 0) == (PurchasePriority.HIGH, 1)
    assert R._decide(HoldingStatus.OWNED_PHYSICAL, True, 0, 1) == (PurchasePriority.SKIP, 0)
