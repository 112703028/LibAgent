from datetime import datetime, timezone

from library_agent import progress
from library_agent.graph import NODE_NAMES, _init_run, _mark_status, _with_tracking


class _FakeQuery:
    def __init__(self, session):
        self._session = session

    def where(self, *a, **k):
        return self


class _FakeSession:
    """比照 tests/test_validator.py 的 _FakeSession 模式：記錄呼叫順序與寫入內容。"""

    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_node_names_order_matches_graph_topology():
    assert NODE_NAMES == [
        "crawler", "parser", "discoverer", "validator", "match_auditor",
        "librarian", "human_review", "recommender",
    ]


def test_mark_status_running_sets_started_at(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    now = datetime.now(timezone.utc)
    _mark_status("run1", "crawler", "running", started_at=now)

    assert fake.committed is True
    assert len(fake.added) == 1
    row = fake.added[0]
    assert row.run_id == "run1"
    assert row.node_name == "crawler"
    assert row.status == "running"
    assert row.started_at == now


def test_mark_status_error_sets_error_message(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    _mark_status("run1", "validator", "error", error="boom")

    row = fake.added[0]
    assert row.status == "error"
    assert row.error == "boom"


def test_init_run_writes_pending_for_all_nodes(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    _init_run("run1")

    assert len(fake.added) == len(NODE_NAMES)
    assert all(row.status == "pending" for row in fake.added)
    assert [row.node_name for row in fake.added] == NODE_NAMES


def test_with_tracking_success_marks_running_then_done(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    def fn(state):
        return {"result": "ok"}

    wrapped = _with_tracking("crawler", fn, "run1")
    result = wrapped({})

    assert result == {"result": "ok"}
    assert len(fake.added) == 2
    assert fake.added[0].status == "running"
    assert fake.added[1].status == "done"
    assert all(row.node_name == "crawler" for row in fake.added)


def test_with_tracking_error_marks_error_and_reraises(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    def fn(state):
        raise ValueError("boom")

    wrapped = _with_tracking("validator", fn, "run1")

    import pytest
    with pytest.raises(ValueError, match="boom"):
        wrapped({})

    assert len(fake.added) == 2
    assert fake.added[0].status == "running"
    assert fake.added[1].status == "error"
    assert fake.added[1].error == "boom"


def test_with_tracking_binds_progress_during_fn_and_unbinds_after(monkeypatch):
    # 節點內的 report() 不帶任何參數，靠這個綁定才知道要寫到哪個 run 的哪個節點
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: _FakeSession())
    seen = {}

    def fn(state):
        seen["binding"] = progress._current.get()
        return {}

    _with_tracking("validator", fn, "run1")({})

    assert seen["binding"].run_id == "run1"
    assert seen["binding"].node == "validator"
    assert progress._current.get() is None  # 節點結束後必須解除


def test_with_tracking_unbinds_even_when_fn_raises(monkeypatch):
    # 沒解除的綁定會殘留在這條執行緒，下一個節點的進度就會寫到錯的節點
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)
    seen = {}

    def fn(state):
        seen["during"] = progress._current.get()
        raise ValueError("boom")

    import pytest
    with pytest.raises(ValueError, match="boom"):
        _with_tracking("validator", fn, "run1")({})

    # 先確認「丟例外之前」確實綁定過，否則下面的 None 是空洞地成立（根本沒綁過）
    assert seen["during"] is not None
    assert progress._current.get() is None
    assert fake.added[-1].status == "error"
