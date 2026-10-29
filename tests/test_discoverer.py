from library_agent.agents.discoverer import _needs_discovery


def test_needs_discovery():
    assert _needs_discovery("X", {"X"}) is False   # 已有書目 → 不需 discovery
    assert _needs_discovery("Y", {"X"}) is True     # 沒書目   → 需 discovery
    assert _needs_discovery("Z", set()) is True


from library_agent.agents.discoverer import _build_rediscover_prompt


def test_rediscover_prompt_lists_failed_titles():
    prompt = _build_rediscover_prompt("國文", "課綱內容", ["虛構書A", "虛構書B"])
    assert "虛構書A" in prompt
    assert "虛構書B" in prompt
    # 必須明確要求不要再推薦這些失敗書名
    assert "不" in prompt and ("存在" in prompt or "查證" in prompt)


import json
from library_agent.db.models import Citation, Course, VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.agents.discoverer import _failed_ai_citations


def test_failed_ai_citations_only_picks_ai_notfound_unsuperseded():
    with SessionLocal() as s:
        # Citation.course_id 有 FK 約束到 courses.course_id，須先建立對應課程列
        course = Course(course_id="TESTFB", course_name="測試課程", semester="9999",
                        source_file="test", raw_content="test")
        s.add(course); s.flush()
        # 三筆：AI推薦+not_found（該選）、AI推薦+verified（不選）、AI推薦+not_found但已superseded（不選）
        c1 = Citation(course_id="TESTFB", title="虛構AI書", authors="[]",
                      is_required=False, raw_mention="[AI推薦] 理由", confidence=0.7)
        c2 = Citation(course_id="TESTFB", title="真實AI書", authors="[]",
                      is_required=False, raw_mention="[AI推薦] 理由", confidence=0.7)
        c3 = Citation(course_id="TESTFB", title="已換掉AI書", authors="[]",
                      is_required=False, raw_mention="[AI推薦] 理由", confidence=0.7,
                      superseded_by_iteration=1)
        s.add_all([c1, c2, c3]); s.flush()
        s.add(VerifiedBookDB(citation_id=c1.id, canonical_title="虛構AI書",
                             source="not_found", verified=False))
        s.add(VerifiedBookDB(citation_id=c2.id, canonical_title="真實AI書",
                             source="ncl", verified=True))
        s.add(VerifiedBookDB(citation_id=c3.id, canonical_title="已換掉AI書",
                             source="not_found", verified=False))
        s.commit()
        try:
            failed = _failed_ai_citations(s, "TESTFB")
            titles = {c.title for c in failed}
            assert titles == {"虛構AI書"}
        finally:
            s.query(VerifiedBookDB).filter(
                VerifiedBookDB.citation_id.in_([c1.id, c2.id, c3.id])
            ).delete(synchronize_session=False)
            s.query(Citation).filter(Citation.course_id == "TESTFB").delete(
                synchronize_session=False)
            s.query(Course).filter(Course.course_id == "TESTFB").delete(
                synchronize_session=False)
            s.commit()
