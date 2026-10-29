"""progress.report 的單元測試（不碰真實資料庫）。

report 的三個職責：沒綁定 run 時靜默跳過、節流後把進度寫進該節點「最新一列」、
任何失敗都不能讓 pipeline 中斷。這裡用假 session 記下送出的 SQL，用預排的時間序列
取代 _now，所以不必真的睡眠就能驗證節流。
"""
import pytest

from library_agent import progress


class _FakeSession:
    """記錄 execute 的 statement（編譯後可檢查 params 與 SQL 文字）。"""

    def __init__(self, sink: list, fail: bool = False):
        self._sink = sink
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        if self._fail:
            raise RuntimeError("db down")
        self._sink.append(stmt.compile())

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _clean_binding():
    # contextvar 會在同一個測試行程內延續，每個測試前後都要清掉，避免互相汙染
    progress._current.set(None)
    yield
    progress._current.set(None)


@pytest.fixture
def writes(monkeypatch):
    sink: list = []
    monkeypatch.setattr(progress, "SessionLocal", lambda: _FakeSession(sink))
    return sink


def _clock(monkeypatch, *times: float) -> None:
    it = iter(times)
    monkeypatch.setattr(progress, "_now", lambda: next(it))


def test_report_without_binding_is_noop(monkeypatch):
    def _boom():
        raise AssertionError("沒有綁定 run 時不該碰資料庫")

    monkeypatch.setattr(progress, "SessionLocal", _boom)
    progress.report(1, 2, "x")  # 不得丟例外


def test_first_report_writes_all_fields(writes, monkeypatch):
    _clock(monkeypatch, 0.0)
    progress.bind("run1", "validator")
    progress.report(1, 10, "書A", "本")

    assert len(writes) == 1
    p = writes[0].params
    assert p["processed"] == 1
    assert p["total"] == 10
    assert p["unit"] == "本"
    assert p["message"] == "書A"


def test_report_targets_latest_row_of_run_and_node(writes, monkeypatch):
    # discoverer 在迴路裡會被執行多次，每次都是新的一列；只能更新「最新那列」
    _clock(monkeypatch, 0.0)
    progress.bind("run1", "validator")
    progress.report(1, 10)

    compiled = writes[0]
    assert "max(" in str(compiled).lower()
    assert "run1" in compiled.params.values()
    assert "validator" in compiled.params.values()


def test_reports_within_interval_are_throttled(writes, monkeypatch):
    _clock(monkeypatch, 0.0, 0.1, 0.2)
    progress.bind("run1", "validator")
    for i in (1, 2, 3):
        progress.report(i, 10)

    assert len(writes) == 1


def test_report_after_interval_writes_again(writes, monkeypatch):
    _clock(monkeypatch, 0.0, 0.6)
    progress.bind("run1", "validator")
    progress.report(1, 10)
    progress.report(2, 10)

    assert len(writes) == 2


def test_final_report_always_written(writes, monkeypatch):
    # 即使離上次寫入不到 0.5 秒，processed >= total 也一定要寫，否則畫面會停在 9/10
    _clock(monkeypatch, 0.0, 0.1)
    progress.bind("run1", "validator")
    progress.report(1, 10)
    progress.report(10, 10)

    assert len(writes) == 2
    assert writes[1].params["processed"] == 10


def test_long_message_is_truncated(writes, monkeypatch):
    _clock(monkeypatch, 0.0)
    progress.bind("run1", "validator")
    progress.report(1, 10, "x" * 500)

    assert len(writes[0].params["message"]) == progress._MAX_MESSAGE


def test_db_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(progress, "SessionLocal", lambda: _FakeSession([], fail=True))
    _clock(monkeypatch, 0.0)
    progress.bind("run1", "validator")

    progress.report(1, 10, "x")  # 進度回報是盡力而為，失敗不得讓節點中斷


def test_unbind_stops_writes(writes, monkeypatch):
    _clock(monkeypatch, 0.0, 1.0)
    token = progress.bind("run1", "validator")
    progress.report(1, 10)
    progress.unbind(token)
    progress.report(2, 10)

    assert len(writes) == 1
