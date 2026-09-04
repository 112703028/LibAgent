from library_agent.agents.crawler import _clean_text, _dedup
from library_agent.state import RawSyllabus


def test_clean_text():
    assert _clean_text("Hello_x000D_\r world  ") == "Hello world"
    assert _clean_text("  abc ") == "abc"


def _syl(cid: str, content: str):
    return RawSyllabus(course_id=cid, course_name="c", semester="114-1",
                       source_file="f.xlsx", raw_content=content)


def test_dedup_keeps_last_per_course():
    out = _dedup([_syl("C1", "first"), _syl("C1", "second"), _syl("C2", "other")])
    assert len(out) == 2
    by_id = {s.course_id: s.raw_content for s in out}
    assert by_id["C1"] == "second"   # 後者覆蓋前者
    assert by_id["C2"] == "other"
