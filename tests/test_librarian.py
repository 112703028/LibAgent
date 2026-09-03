from types import SimpleNamespace

from library_agent.agents import librarian as L
from library_agent.agents.librarian import _to_status
from library_agent.state import HoldingCheck, HoldingStatus, VerifiedBook, BookCitation


def _result(found, has_ebook=False, physical_count=0):
    return SimpleNamespace(found=found, has_ebook=has_ebook, physical_count=physical_count)


def test_to_status_missing_when_not_found():
    assert _to_status(_result(found=False)) is HoldingStatus.MISSING


def test_to_status_ebook_takes_priority():
    r = _result(found=True, has_ebook=True, physical_count=5)
    assert _to_status(r) is HoldingStatus.OWNED_EBOOK


def test_to_status_physical():
    r = _result(found=True, has_ebook=False, physical_count=3)
    assert _to_status(r) is HoldingStatus.OWNED_PHYSICAL


def test_to_status_partial_when_found_but_no_copies():
    r = _result(found=True, has_ebook=False, physical_count=0)
    assert _to_status(r) is HoldingStatus.PARTIAL


# _process_one：worker 邊界 — 只算、不碰 DB，例外用 return 而非 raise

def test_process_one_returns_result_without_raising(monkeypatch):
    vb = SimpleNamespace(id=7, canonical_title="X")
    fake_holding = object()
    monkeypatch.setattr(L, "_check_one", lambda v: fake_holding)
    vb_id, holding, error = L._process_one(vb)
    assert vb_id == 7 and holding is fake_holding and error is None


def test_process_one_catches_exception_and_returns_error(monkeypatch):
    vb = SimpleNamespace(id=7, canonical_title="X")

    def boom(v):
        raise RuntimeError("Alma down")

    monkeypatch.setattr(L, "_check_one", boom)
    vb_id, holding, error = L._process_one(vb)
    assert vb_id == 7 and holding is None
    assert error is not None and "Alma down" in error


# _stage_save：cascade 刪舊（recommendations → holding_checks）再寫、不 commit

class _FakeSession:
    def __init__(self, scalars_return=None):
        self.calls: list[str] = []
        self._scalars_return = scalars_return or []

    def scalars(self, *a, **k):
        self.calls.append("scalars")
        return iter(self._scalars_return)

    def execute(self, *a, **k):
        self.calls.append("execute")

    def add(self, *a, **k):
        self.calls.append("add")

    def commit(self):
        self.calls.append("commit")


def _holding():
    cit = BookCitation(course_id="C1", title="t")
    vb = VerifiedBook(citation=cit, canonical_title="T", source="alma")
    return HoldingCheck(book=vb, status=HoldingStatus.OWNED_PHYSICAL, holdings_count=1)


def test_stage_save_cascades_when_downstream_holdings_exist():
    fs = _FakeSession(scalars_return=[500])  # 舊 holding_check id=500 已存在
    L._stage_save(fs, _holding(), 99)
    # 查舊 id → 刪 recommendations → 刪 holding_checks → 寫新，全程無 commit
    assert fs.calls == ["scalars", "execute", "execute", "add"]


def test_stage_save_no_cascade_when_no_prior_holding():
    fs = _FakeSession(scalars_return=[])
    L._stage_save(fs, _holding(), 99)
    assert fs.calls == ["scalars", "add"]  # 沒舊列 → 不必刪，直接寫
