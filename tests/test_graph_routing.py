from library_agent.graph import _route_after_validator, _MAX_DISCOVERY_ITERATIONS
from library_agent.db.models import Citation, Course, VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal


def _seed_failed_ai(course_id):
    with SessionLocal() as s:
        # Citation.course_id 有 FK 約束到 courses.course_id，須先建立對應課程列
        course = Course(course_id=course_id, course_name="測試課程", semester="9999",
                         source_file="test", raw_content="test")
        s.add(course); s.flush()
        c = Citation(course_id=course_id, title="虛構書X", authors="[]",
                     is_required=False, raw_mention="[AI推薦] 理由", confidence=0.7)
        s.add(c); s.flush()
        s.add(VerifiedBookDB(citation_id=c.id, canonical_title="虛構書X",
                             source="not_found", verified=False))
        s.commit()
        return c.id


def _cleanup(course_id):
    with SessionLocal() as s:
        ids = [r for (r,) in s.query(Citation.id).filter(Citation.course_id == course_id)]
        s.query(VerifiedBookDB).filter(VerifiedBookDB.citation_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(Citation).filter(Citation.course_id == course_id).delete(
            synchronize_session=False)
        s.query(Course).filter(Course.course_id == course_id).delete(
            synchronize_session=False)
        s.commit()


def test_route_to_discoverer_when_failed_ai_and_under_limit():
    _seed_failed_ai("TESTROUTE")
    try:
        state = {"course_ids": ["TESTROUTE"], "discovery_iteration": 0}
        assert _route_after_validator(state) == "discoverer"
    finally:
        _cleanup("TESTROUTE")


def test_route_to_match_auditor_when_iteration_at_limit():
    _seed_failed_ai("TESTROUTE2")
    try:
        state = {"course_ids": ["TESTROUTE2"], "discovery_iteration": _MAX_DISCOVERY_ITERATIONS}
        assert _route_after_validator(state) == "match_auditor"
    finally:
        _cleanup("TESTROUTE2")


def test_route_to_match_auditor_when_no_failed_ai():
    state = {"course_ids": ["NONEXISTENT_COURSE"], "discovery_iteration": 0}
    assert _route_after_validator(state) == "match_auditor"
