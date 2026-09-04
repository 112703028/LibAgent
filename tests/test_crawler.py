from library_agent.agents.crawler import _clean_text, _dedup, _pick_unprocessed
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


# _pick_unprocessed：挑前 N 筆還沒處理過的新課

def test_pick_unprocessed_takes_first_n_new():
    syllabi = [_syl("C1", "book A"), _syl("C2", "book B"), _syl("C3", "book C")]
    assert _pick_unprocessed(syllabi, set(), 2) == ["C1", "C2"]


def test_pick_unprocessed_skips_already_parsed():
    # C1 已有 Citation → 跳過，往後選 C2、C3
    syllabi = [_syl("C1", "book A"), _syl("C2", "book B"), _syl("C3", "book C")]
    assert _pick_unprocessed(syllabi, {"C1"}, 2) == ["C2", "C3"]


def test_pick_unprocessed_skips_placeholder():
    # C1 是佔位符（parser 永遠不會產生 Citation）→ 不佔用名額，選 C2、C3
    syllabi = [_syl("C1", "TBD"), _syl("C2", "book B"), _syl("C3", "book C")]
    assert _pick_unprocessed(syllabi, set(), 2) == ["C2", "C3"]


def test_pick_unprocessed_returns_fewer_when_not_enough_new():
    # 只剩 C3 是新課 → 就算 limit=2 也只回傳 1 筆（不會湊數選到已處理的）
    syllabi = [_syl("C1", "book A"), _syl("C2", "TBD"), _syl("C3", "book C")]
    assert _pick_unprocessed(syllabi, {"C1"}, 2) == ["C3"]
