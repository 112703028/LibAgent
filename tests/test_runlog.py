"""runlog：把 print() / stderr 的輸出多存一份，按行記下來，供 dashboard 顯示。

只記 pipeline 自己 print 出來的行；uvicorn 的存取日誌走它自己持有的輸出管道，不經過
sys.stdout，天生不會進來（見 docs/superpowers/specs/2026-10-29-...-design.md 第 5 點）。
"""
import io
import sys
import threading

import pytest

from library_agent import runlog


@pytest.fixture(autouse=True)
def _clean():
    runlog.uninstall()
    runlog.reset()
    yield
    runlog.uninstall()
    runlog.reset()


def _lines() -> list[str]:
    return runlog.read()["lines"]


def test_install_tees_stdout_and_records_complete_lines(monkeypatch):
    real = io.StringIO()
    monkeypatch.setattr(sys, "stdout", real)
    runlog.install()
    try:
        print("a")
        print("b", end="")   # print 會把文字與換行分兩次 write；沒換行的半行要等換行才算一行
        print(" c")
    finally:
        runlog.uninstall()

    assert real.getvalue() == "a\nb c\n"        # 終端機的輸出一個字都不變
    assert _lines() == ["a", "b c"]


def test_uninstall_restores_original_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    before_out, before_err = sys.stdout, sys.stderr

    runlog.install()
    assert sys.stdout is not before_out and sys.stderr is not before_err
    runlog.uninstall()

    assert sys.stdout is before_out and sys.stderr is before_err


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    before = sys.stdout
    runlog.install()
    runlog.install()          # 重複安裝不得把「已經是 Tee 的 stdout」當成原始輸出
    runlog.uninstall()

    assert sys.stdout is before


def test_stderr_is_recorded_too(monkeypatch):
    # traceback 走 stderr；不收的話頁面上看不到錯誤
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    runlog.install()
    try:
        print("Traceback: boom", file=sys.stderr)
    finally:
        runlog.uninstall()

    assert _lines() == ["Traceback: boom"]


def test_blank_lines_are_not_recorded(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.install()
    try:
        print()
        print("  ")
        print("x")
    finally:
        runlog.uninstall()

    assert _lines() == ["x"]


def test_long_line_is_truncated_in_log_but_not_in_terminal(monkeypatch):
    real = io.StringIO()
    monkeypatch.setattr(sys, "stdout", real)
    runlog.install()
    try:
        print("x" * 3000)
    finally:
        runlog.uninstall()

    assert len(_lines()[0]) == runlog._MAX_LINE
    assert len(real.getvalue()) == 3001


def test_lines_are_prefixed_with_the_bound_node(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.install()
    try:
        token = runlog.bind_node("validator")
        print("OK [    1/65] 書名")
        runlog.unbind_node(token)
        print("無節點")
    finally:
        runlog.uninstall()

    assert _lines() == ["[validator] OK [    1/65] 書名", "無節點"]


def test_worker_thread_print_uses_the_latest_running_node(monkeypatch):
    # ThreadPoolExecutor 的 worker 不會繼承 contextvar，它們印的行改用「最近開始、還在跑」的節點
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.install()
    try:
        t_a = runlog.bind_node("match_auditor")
        t_b = runlog.bind_node("librarian")

        def worker(text):
            print(text)

        for text in ("w1", "w2"):
            th = threading.Thread(target=worker, args=(text,))
            th.start()
            th.join()
            if text == "w1":
                runlog.unbind_node(t_b)          # librarian 結束後，剩 match_auditor 還在跑
        runlog.unbind_node(t_a)
    finally:
        runlog.uninstall()

    assert _lines() == ["[librarian] w1", "[match_auditor] w2"]


def test_read_after_returns_only_newer_lines(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.install()
    try:
        print("one")
        print("two")
        cursor = runlog.read()["last"]
        print("three")
    finally:
        runlog.uninstall()

    assert runlog.read(after=cursor)["lines"] == ["three"]
    assert runlog.read(after=runlog.read()["last"])["lines"] == []


def test_buffer_is_bounded_and_keeps_the_latest(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(runlog, "MAX_LINES", 5)
    runlog.install()
    try:
        for i in range(8):
            print(f"line{i}")
    finally:
        runlog.uninstall()

    assert _lines() == [f"line{i}" for i in range(3, 8)]


def test_reset_clears_lines_and_changes_epoch(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.install()
    try:
        print("old run")
    finally:
        runlog.uninstall()
    old_epoch = runlog.read()["epoch"]

    runlog.reset()

    after = runlog.read()
    assert after["lines"] == []
    assert after["epoch"] != old_epoch


def test_stale_epoch_gets_the_whole_buffer(monkeypatch):
    # 前端游標是上一次執行的流水號；新執行開始後（epoch 變了）要整份重拿，不能用舊游標漏掉新行
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    runlog.reset()
    stale_epoch = runlog.read()["epoch"]
    runlog.reset()
    runlog.install()
    try:
        print("new run line")
    finally:
        runlog.uninstall()
    huge_cursor = runlog.read()["last"] + 1000

    got = runlog.read(after=huge_cursor, epoch=stale_epoch)

    assert got["lines"] == ["new run line"]
    assert got["epoch"] != stale_epoch
