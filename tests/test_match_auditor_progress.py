"""match_auditor 的進度回報：規則層直接判掉的也要算進「已處理」。

進度條的分母是本次全部待稽核筆數。若只在 LLM 迴圈裡計數，規則層先判掉的那批
不會被算進去，進度條會從 0 開始、最後卻停在比 100% 少的位置。

沿用 tests/test_graph_routing.py 的作法：用真實 DB 播種專用課程、結束時清掉，
並以 course_ids 限定範圍，確保只動到自己播種的資料。LLM 與 OpenAI client 都換掉，不花錢。
"""
from library_agent.agents import match_auditor
from library_agent.db.models import Citation, Course, VerifiedBook
from library_agent.db.session import SessionLocal

COURSE_ID = "TESTAUDPROG"


def _seed():
    with SessionLocal() as s:
        s.add(Course(course_id=COURSE_ID, course_name="測試課程", semester="9999",
                     source_file="test", raw_content="test"))
        s.flush()
        # 兩筆：一筆書名完全不相干（規則層直接判配錯、不呼叫 LLM），一筆書名相同（交 LLM 複核）
        for title, matched in (("經濟學原理", "社會學"), ("微積分", "微積分")):
            c = Citation(course_id=COURSE_ID, title=title, authors="[]", is_required=True,
                         raw_mention="指定", confidence=0.9)
            s.add(c)
            s.flush()
            s.add(VerifiedBook(citation_id=c.id, canonical_title=matched,
                               canonical_authors="[]", source="nla", verified=True))
        s.commit()


def _cleanup():
    with SessionLocal() as s:
        ids = [i for (i,) in s.query(Citation.id).filter(Citation.course_id == COURSE_ID)]
        s.query(VerifiedBook).filter(VerifiedBook.citation_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(Citation).filter(Citation.course_id == COURSE_ID).delete(
            synchronize_session=False)
        s.query(Course).filter(Course.course_id == COURSE_ID).delete(
            synchronize_session=False)
        s.commit()


def test_reports_progress_over_all_items(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(match_auditor, "report",
                        lambda processed, total, message=None, unit="本": calls.append((processed, total)))
    monkeypatch.setattr(match_auditor, "_llm_same_book", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr("openai.OpenAI", lambda **kw: object())

    _seed()
    try:
        match_auditor.match_auditor_node({"course_ids": [COURSE_ID]})
    finally:
        _cleanup()

    assert calls, "沒有任何進度回報"
    assert all(total == 2 for _, total in calls)          # 分母是全部 2 筆，不是只算 LLM 那 1 筆
    processed = [p for p, _ in calls]
    assert processed == sorted(processed)                  # 單調不減
    assert calls[0] == (1, 2)                              # 規則層判掉 1 筆，一開始就算 1 筆已處理
    assert calls[-1] == (2, 2)                             # 結束時走到 100%
