import json
import traceback

from openai import OpenAI
from sqlalchemy import select

from library_agent.config import get_settings
from library_agent.db.models import Citation, Course
from library_agent.db.session import SessionLocal
from library_agent.integrations.nccu_syllabus import fetch_syllabus, fetch_student_number
from library_agent.prompts.discoverer_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from library_agent.state import AgentState, BookCitation

_settings = get_settings()
_client = OpenAI(api_key=_settings.openai_api_key)

_AI_CONFIDENCE_CAP = 0.75


def _existing_titles(course_id: str) -> list[str]:
    with SessionLocal() as session:
        return list(session.scalars(
            select(Citation.title).where(Citation.course_id == course_id)
        ).all())


def _needs_discovery(course_id: str) -> bool:
    """只處理 parser 完全沒抓到書目的課程。"""
    with SessionLocal() as session:
        existing = session.scalars(
            select(Citation).where(Citation.course_id == course_id)
        ).first()
    return existing is None


def _discover_one(course: Course) -> list[BookCitation]:
    syllabus_text = fetch_syllabus(course.course_id, course.semester)
    if not syllabus_text or not syllabus_text.strip():
        return []

    existing = _existing_titles(course.course_id)
    existing_str = "\n".join(f"- {t}" for t in existing) if existing else "（無）"

    user_prompt = USER_PROMPT_TEMPLATE.format(
        course_name=course.course_name,
        syllabus_text=syllabus_text[:3000],
        existing_titles=existing_str,
    )
    response = _client.chat.completions.create(
        model=_settings.llm_model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    data = json.loads(response.choices[0].message.content)

    citations = []
    for book in data.get("books", []):
        if not book.get("title"):
            continue
        confidence = min(float(book.get("confidence", 0.65)), _AI_CONFIDENCE_CAP)
        citations.append(BookCitation(
            course_id=course.course_id,
            title=book.get("title", ""),
            authors=book.get("authors") or [],
            edition=book.get("edition"),
            isbn=book.get("isbn"),
            publisher=book.get("publisher"),
            year=book.get("year"),
            is_required=False,
            raw_mention=book.get("raw_mention", "[AI推薦]"),
            confidence=confidence,
        ))
    return citations


def _save_citations(citations: list[BookCitation]) -> None:
    with SessionLocal() as session:
        for c in citations:
            session.add(Citation(
                course_id=c.course_id,
                title=c.title,
                authors=json.dumps(c.authors, ensure_ascii=False),
                edition=c.edition,
                isbn=c.isbn,
                publisher=c.publisher,
                year=c.year,
                is_required=c.is_required,
                raw_mention=c.raw_mention,
                confidence=c.confidence,
            ))
        session.commit()


def discoverer_node(_state: AgentState) -> AgentState:
    all_citations: list[BookCitation] = []
    errors: list[str] = []

    with SessionLocal() as session:
        courses = session.scalars(select(Course)).all()

    total = len(courses)
    for i, course in enumerate(courses, 1):
        label = f"[{i:>4}/{total}] {course.course_id} {course.course_name}"

        # 如果課程已經有推薦書目就跳過
        if not _needs_discovery(course.course_id):
            print(f"  SKIP  {label}")
            continue
        try:
            citations = _discover_one(course)
            if citations:
                _save_citations(citations)
                print(f"  OK    {label}  → {len(citations)} 筆推薦書目")
                all_citations.extend(citations)
            else:
                print(f"  EMPTY {label}  （課綱無足夠內容）")
        except Exception as e:
            errors.append(f"[discoverer] {course.course_id}: {e}")
            print(f"  ERROR {label}  → {e}")
            traceback.print_exc()

    return {"citations": all_citations, "errors": errors}


if __name__ == "__main__":
    import sys
    _TEST_LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    with SessionLocal() as session:
        courses = session.scalars(
            select(Course)
            .outerjoin(Citation, Citation.course_id == Course.course_id)
            .where(Citation.id == None)  # noqa: E711
            .limit(_TEST_LIMIT)
        ).all()

    print(f"測試 {len(courses)} 筆無書目課程")
    for course in courses:
        print(f"\n  課程：{course.course_name}（{course.course_id}）")
        try:
            citations = _discover_one(course)
            if citations:
                for c in citations:
                    print(f"    推薦：{c.title}  confidence={c.confidence:.2f}")
            else:
                print("    （無推薦）")
        except Exception as e:
            print(f"    ERROR: {e}")
            traceback.print_exc()
