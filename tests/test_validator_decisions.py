"""validator 不重驗已有人工決定的書：查無此書的書每次執行都會重驗，但人工核准／退回的不能被洗掉。"""
from library_agent.agents import validator as V
from library_agent.db.models import Citation, Course, VerifiedBook


def test_validator_does_not_retry_books_with_a_human_decision(isolated_db, monkeypatch):
    with isolated_db() as s:
        s.add(Course(course_id="V1", course_name="課-V1", semester="114-1", source_file="t", raw_content="t"))
        s.flush()
        for status, choice in (("approved", "custom"), ("rejected", "none")):
            c = Citation(course_id="V1", title=f"書-{status}", authors="[]", is_required=True,
                         raw_mention="x", confidence=0.9)
            s.add(c)
            s.flush()
            s.add(VerifiedBook(citation_id=c.id, canonical_title=c.title, canonical_authors="[]",
                               source="ncl_not_found", verified=False, review_status=status,
                               review_choice=choice))
        s.commit()

    def boom(*args, **kwargs):
        raise AssertionError("已有人工決定的書不該重新驗證")

    monkeypatch.setattr(V, "SessionLocal", isolated_db)
    monkeypatch.setattr(V, "_validate_one", boom)

    V.validator_node({})

    with isolated_db() as s:
        rows = s.query(VerifiedBook.review_status, VerifiedBook.review_choice).order_by(VerifiedBook.id).all()
    assert rows == [("approved", "custom"), ("rejected", "none")]
