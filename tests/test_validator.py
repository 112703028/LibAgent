from types import SimpleNamespace

from library_agent.agents.validator import _CacheEntry, _is_chinese, _process_one, _stage_save
from library_agent.state import BookCitation, VerifiedBook


def test_is_chinese():
    assert _is_chinese("經濟學") is True
    assert _is_chinese("Economics") is False
    assert _is_chinese("Python 程式設計") is True


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
