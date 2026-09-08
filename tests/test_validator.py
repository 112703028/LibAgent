from types import SimpleNamespace

from library_agent.agents import validator as V
from library_agent.agents.validator import (
    _CacheEntry, _is_chinese, _process_one, _stage_save, _validate_chinese, _validate_english,
)
from library_agent.integrations.google_books import QuotaExceededError
from library_agent.state import BookCitation, VerifiedBook


def test_is_chinese():
    assert _is_chinese("經濟學") is True
    assert _is_chinese("Economics") is False
    assert _is_chinese("Python 程式設計") is True


# _validate_chinese：NCL 優先，miss 才走 Google（不打真實網路，全 monkeypatch）

def _book_record(source):
    return SimpleNamespace(canonical_title="正規書名", canonical_authors=["作者"],
                           isbn_13="9789570000000", source=source)


def _cn_citation():
    return BookCitation(course_id="C1", title="經濟學", confidence=0.9)


def test_validate_chinese_ncl_hit_skips_google(monkeypatch):
    calls = {"google": 0}
    monkeypatch.setattr(V, "nla_lookup", lambda **kw: _book_record("nla"))
    monkeypatch.setattr(V, "google_lookup", lambda **kw: calls.__setitem__("google", calls["google"] + 1) or _book_record("google_books"))

    out = _validate_chinese(_cn_citation(), low_confidence=False)
    assert out.verified is True
    assert out.source == "nla"
    assert calls["google"] == 0  # NCL 命中就不該打 Google（省配額）


def test_validate_chinese_falls_back_to_google_on_ncl_miss(monkeypatch):
    monkeypatch.setattr(V, "nla_lookup", lambda **kw: None)          # NCL 查不到
    monkeypatch.setattr(V, "google_lookup", lambda **kw: _book_record("google_books"))

    out = _validate_chinese(_cn_citation(), low_confidence=False)
    assert out.verified is True
    assert out.source == "google_books"   # source 反映實際命中來源


def test_validate_chinese_both_miss_is_not_found(monkeypatch):
    monkeypatch.setattr(V, "nla_lookup", lambda **kw: None)
    monkeypatch.setattr(V, "google_lookup", lambda **kw: None)

    out = _validate_chinese(_cn_citation(), low_confidence=False)
    assert out.verified is False
    assert out.source == "ncl_not_found"
    assert out.requires_human_review is True


def _raise_quota(**kw):
    raise QuotaExceededError("rate limited")


def test_validate_chinese_quota_exceeded_marks_special_source(monkeypatch):
    # NCL miss → Google 配額爆 → 標 quota_exceeded（非 ncl_not_found），verified=False 待重驗
    monkeypatch.setattr(V, "nla_lookup", lambda **kw: None)
    monkeypatch.setattr(V, "google_lookup", _raise_quota)

    out = _validate_chinese(_cn_citation(), low_confidence=False)
    assert out.verified is False
    assert out.source == "quota_exceeded"
    assert out.requires_human_review is True


def test_validate_english_quota_exceeded_marks_special_source(monkeypatch):
    # LOC miss → Google 配額爆 → 英文書也標 quota_exceeded（與中文書一致）
    monkeypatch.setattr(V, "loc_lookup", lambda **kw: None)
    monkeypatch.setattr(V, "google_lookup", _raise_quota)

    cit = BookCitation(course_id="C1", title="Clean Code", confidence=0.9)
    out = _validate_english(cit, low_confidence=False)
    assert out.verified is False
    assert out.source == "quota_exceeded"
    assert out.requires_human_review is True


def test_validate_english_loc_hit_skips_google(monkeypatch):
    # LOC 命中就不該打 Google（回歸：確認 fallback 只在 miss 時觸發）
    calls = {"google": 0}
    monkeypatch.setattr(V, "loc_lookup", lambda **kw: _book_record("loc"))
    monkeypatch.setattr(V, "google_lookup", lambda **kw: calls.__setitem__("google", calls["google"] + 1))

    cit = BookCitation(course_id="C1", title="Clean Code", confidence=0.9)
    out = _validate_english(cit, low_confidence=False)
    assert out.verified is True
    assert out.source == "loc"
    assert calls["google"] == 0


def _fake_citation(**kw):
    base = dict(id=1, course_id="C1", title="Clean Code", authors=None, edition=None,
                isbn=None, publisher=None, year=2008, is_required=True,
                raw_mention="", confidence=0.9)
    base.update(kw)
    return SimpleNamespace(**base)


def test_process_one_skips_when_in_done_ids():
    assert _process_one(_fake_citation(id=5), {}, {5}) == (None, None, False)


def test_process_one_cache_hit_returns_without_network():
    c = _fake_citation(id=7, isbn="9780132350884")
    cache = {"isbn:9780132350884": _CacheEntry(
        canonical_title="Clean Code", canonical_authors=["Robert C. Martin"],
        isbn_13="9780132350884", source="loc")}
    verified, error, from_cache = _process_one(c, cache, set())
    assert error is None
    assert from_cache is True
    assert verified.verified is True
    assert verified.canonical_title == "Clean Code"
    assert verified.source == "loc"


class _FakeSession:
    """scalar_return 模擬「舊 verified_books.id」查詢結果；
    scalars_return 模擬「引用它的 holding_check id 清單」查詢結果。"""

    def __init__(self, scalar_return=None, scalars_return=None):
        self.calls: list[str] = []
        self.added: list = []
        self._scalar_return = scalar_return
        self._scalars_return = scalars_return or []

    def scalar(self, *a, **k):
        self.calls.append("scalar")
        return self._scalar_return

    def scalars(self, *a, **k):
        self.calls.append("scalars")
        return iter(self._scalars_return)

    def execute(self, *a, **k):
        self.calls.append("execute")

    def add(self, obj):
        self.calls.append("add")
        self.added.append(obj)

    def commit(self):
        self.calls.append("commit")


def _verified(requires_review: bool):
    cit = BookCitation(course_id="C1", title="t")
    return VerifiedBook(citation=cit, canonical_title="T", source="loc",
                        verified=True, requires_human_review=requires_review)


def test_stage_save_no_prior_row_just_inserts():
    fs = _FakeSession(scalar_return=None)  # 這個 citation 從沒驗證過
    _stage_save(fs, _verified(requires_review=True), 1)
    assert fs.calls == ["scalar", "execute", "add"]  # 查舊列(無) → 刪(保險) → 寫，無 commit
    assert fs.added[0].review_status == "pending"


def test_stage_save_no_review_status_when_not_flagged():
    fs = _FakeSession(scalar_return=None)
    _stage_save(fs, _verified(requires_review=False), 1)
    assert fs.added[0].review_status is None


def test_stage_save_cascades_when_downstream_rows_exist():
    # 回歸測試：舊 verified_books（id=99）已被 librarian/recommender 引用
    # （holding_check id=500）。這正是實際撞到的 FK violation 場景 ——
    # verified=False 的書（MISS/not_found）每次重跑都會被 retry（見 _process_one），
    # 若不先由下往上 cascade 就直接 DELETE verified_books，會被 FK 約束擋下。
    fs = _FakeSession(scalar_return=99, scalars_return=[500])
    _stage_save(fs, _verified(requires_review=True), 1)
    assert fs.calls == ["scalar", "scalars", "execute", "execute", "execute", "add"]
    # 三次 execute 依序：刪 recommendations → 刪 holding_checks → 刪 verified_books


def test_stage_save_no_cascade_when_old_row_has_no_downstream():
    # 舊列存在，但還沒被 librarian 處理過 → 沒有 holding_check，不必多刪兩層
    fs = _FakeSession(scalar_return=99, scalars_return=[])
    _stage_save(fs, _verified(requires_review=True), 1)
    assert fs.calls == ["scalar", "scalars", "execute", "add"]
