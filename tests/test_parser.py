from types import SimpleNamespace

from library_agent.agents import parser as P
from library_agent.agents.parser import _needs_parsing, _parse_year
from library_agent.state import BookCitation


def test_parse_year_western_passthrough():
    assert _parse_year(2020) == 2020
    assert _parse_year("2019") == 2019
    assert _parse_year(2021.0) == 2021


def test_parse_year_minguo_conversion():
    assert _parse_year("民89") == 2000       # 89 + 1911
    assert _parse_year("民國113") == 2024     # 113 + 1911
    assert _parse_year("ROC89") == 2000


def test_parse_year_none_and_garbage():
    assert _parse_year(None) is None
    assert _parse_year("待補") is None


def test_needs_parsing_placeholder_skips():
    assert _needs_parsing(SimpleNamespace(course_id="X", raw_content="n/a"), set()) is False
    assert _needs_parsing(SimpleNamespace(course_id="X", raw_content="  N/A "), set()) is False


def test_needs_parsing_already_has_citations_skips():
    c = SimpleNamespace(course_id="X", raw_content="some book list")
    assert _needs_parsing(c, {"X"}) is False


def test_needs_parsing_new_course_parses():
    c = SimpleNamespace(course_id="X", raw_content="some book list")
    assert _needs_parsing(c, set()) is True


# _process_one：worker 邊界 — 只算、不碰 DB，例外用 return 而非 raise

def test_process_one_returns_result_without_raising(monkeypatch):
    course = SimpleNamespace(course_id="X", course_name="c", raw_content="books")
    fake_citations = [BookCitation(course_id="X", title="Clean Code")]
    monkeypatch.setattr(P, "_parse_one", lambda c: fake_citations)

    course_id, citations, error = P._process_one(course)
    assert course_id == "X"
    assert citations == fake_citations
    assert error is None


def test_process_one_catches_exception_and_returns_error(monkeypatch):
    course = SimpleNamespace(course_id="X", course_name="c", raw_content="books")

    def boom(c):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(P, "_parse_one", boom)

    course_id, citations, error = P._process_one(course)
    assert course_id == "X"
    assert citations is None
    assert error is not None and "LLM down" in error


# _stage_citations：先刪再寫、不 commit（跟 validator._stage_save 同款契約）

class _FakeSession:
    def __init__(self):
        self.calls: list[str] = []

    def execute(self, *a, **k):
        self.calls.append("execute")

    def add(self, *a, **k):
        self.calls.append("add")

    def commit(self):
        self.calls.append("commit")


def test_stage_citations_stages_without_commit():
    fs = _FakeSession()
    citations = [BookCitation(course_id="X", title="A"), BookCitation(course_id="X", title="B")]
    P._stage_citations(fs, "X", citations)
    assert fs.calls == ["execute", "add", "add"]  # 先刪一次，再各寫一筆，且無 commit
