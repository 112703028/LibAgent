"""dashboard 的執行紀錄（終端文字 log）：取代 mermaid 流程圖之後的頁面行為。"""
import pytest

from library_agent import dashboard


def test_render_has_no_mermaid_or_static():
    html = dashboard._render(dashboard._load_data())

    assert "mermaid" not in html
    assert "/static/" not in html


def test_app_has_no_static_mount():
    # /static 目錄只剩 mermaid.min.js，檔案刪除後目錄不存在，掛載會讓 import 失敗
    assert "/static" not in [getattr(r, "path", None) for r in dashboard.app.routes]


# ---------- GET /log 與 pipeline 接線 ----------

import io
import sys

from fastapi.testclient import TestClient

from library_agent import runlog


@pytest.fixture
def clean_runlog():
    runlog.uninstall()
    runlog.reset()
    yield
    runlog.uninstall()
    runlog.reset()


def _fresh_run_state(monkeypatch):
    monkeypatch.setattr(dashboard, "_run_state", {
        "status": "running", "started_at": None, "finished_at": None, "error": None, "run_id": "X"})


def test_log_endpoint_returns_only_lines_after_the_cursor(clean_runlog, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.install()
    try:
        print("first")
        client = TestClient(dashboard.app)
        body = client.get("/log", params={"after": 0}).json()
        assert "first" in body["lines"]

        print("second")
        body2 = client.get("/log", params={"after": body["last"], "epoch": body["epoch"]}).json()
    finally:
        runlog.uninstall()

    assert body2["lines"] == ["second"]
    assert body2["epoch"] == body["epoch"]


def test_log_endpoint_gives_whole_buffer_when_epoch_is_stale(clean_runlog):
    client = TestClient(dashboard.app)
    stale = client.get("/log").json()["epoch"]
    runlog.reset()                       # 新的一次執行開始
    runlog._append("new run line")

    body = client.get("/log", params={"after": 10**9, "epoch": stale}).json()

    assert body["lines"] == ["new run line"]
    assert body["epoch"] != stale


def test_run_pipeline_records_output_and_restores_streams(clean_runlog, monkeypatch):
    class _FakeGraph:
        def invoke(self, initial):
            print("[    1/2] 書A")

    monkeypatch.setattr("library_agent.graph.build_graph", lambda run_id: _FakeGraph())
    _fresh_run_state(monkeypatch)
    before_out, before_err = sys.stdout, sys.stderr

    dashboard._run_pipeline("X", None, None)

    assert sys.stdout is before_out and sys.stderr is before_err
    assert "[    1/2] 書A" in runlog.read()["lines"]
    assert dashboard._run_state["status"] == "done"


def test_run_pipeline_failure_shows_traceback_and_restores_streams(clean_runlog, monkeypatch):
    class _BoomGraph:
        def invoke(self, initial):
            raise RuntimeError("boom")

    monkeypatch.setattr("library_agent.graph.build_graph", lambda run_id: _BoomGraph())
    _fresh_run_state(monkeypatch)
    before_out, before_err = sys.stdout, sys.stderr

    dashboard._run_pipeline("X", None, None)

    assert sys.stdout is before_out and sys.stderr is before_err   # 失敗也要還原
    assert dashboard._run_state["status"] == "error"
    # 錯誤要看得到出在哪：traceback 的最後一行會出現在網頁 log
    assert any("RuntimeError: boom" in line for line in runlog.read()["lines"])


def test_each_run_starts_with_a_clean_log(clean_runlog, monkeypatch):
    class _Graph:
        def __init__(self, text):
            self.text = text

        def invoke(self, initial):
            print(self.text)

    _fresh_run_state(monkeypatch)
    monkeypatch.setattr("library_agent.graph.build_graph", lambda run_id: _Graph("old run"))
    dashboard._run_pipeline("X", None, None)
    monkeypatch.setattr("library_agent.graph.build_graph", lambda run_id: _Graph("new run"))
    _fresh_run_state(monkeypatch)
    dashboard._run_pipeline("Y", None, None)

    assert runlog.read()["lines"] == ["new run"]
