from datetime import datetime, timezone

from library_agent.graph import NODE_NAMES, _init_run, _mark_status


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
        "crawler", "parser", "discoverer", "validator",
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
