import json
import traceback

from openai import OpenAI
from sqlalchemy import delete, select

from library_agent.config import get_settings
from library_agent.db.models import Citation, Course
from library_agent.db.session import SessionLocal
from library_agent.prompts.parser_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from library_agent.state import AgentState, BookCitation
from library_agent.integrations.nccu_syllabus import fetch_syllabus_pdf

_settings = get_settings()
_client = OpenAI(api_key=_settings.openai_api_key)

# 把民國轉成西元
def _parse_year(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        s = str(raw).strip()
        # 民國年：民89 / 民國89 / ROC89 → +1911
        import re
        m = re.search(r"(\d+)", s)
        if m:
            year = int(m.group(1))
            if year < 200:  # 民國年不超過 200
                return year + 1911
            return year
        return None


_NEEDS_PDF_SYSTEM = (
    "你是課程大綱分析助手。判斷以下書目欄位是否只是告知「書單在附件/上傳檔案中」，"
    "而非直接列出書目內容。若是，回傳 {\"needs_pdf\": true}；否則回傳 {\"needs_pdf\": false}。"
    "只回傳 JSON，不要其他文字。"
)


def _needs_pdf_fetch(raw_content: str) -> bool:
    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _NEEDS_PDF_SYSTEM},
            {"role": "user", "content": raw_content},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return bool(data.get("needs_pdf", False))


def _parse_one(course: Course) -> list[BookCitation]:
    raw_content = course.raw_content
    if _needs_pdf_fetch(raw_content):
        pdf_text = fetch_syllabus_pdf(course.course_id, course.semester)
        if pdf_text:
            raw_content = pdf_text

    user_prompt = USER_PROMPT_TEMPLATE.format(
        course_name=course.course_name,
        raw_content=raw_content,
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
        citations.append(BookCitation(
            course_id=course.course_id,
            title=book.get("title", ""),
            authors=book.get("authors") or [],
            edition=book.get("edition"),
            isbn=book.get("isbn"),
            publisher=book.get("publisher"),
            year=_parse_year(book.get("year")),
            is_required=book.get("is_required", True),
            raw_mention=book.get("raw_mention", ""),
            confidence=float(book.get("confidence", 0.0)),
        ))
    return citations


def _save_citations(course_id: str, citations: list[BookCitation]) -> None:
    with SessionLocal() as session:
        session.execute(delete(Citation).where(Citation.course_id == course_id))
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


_PLACEHOLDER = {"tbd", "n/a", "na", "none", "待補", "待定", ""}

def _needs_parsing(session, course: Course) -> bool:
    if course.raw_content.strip().lower() in _PLACEHOLDER:
        return False
    existing = session.scalars(
        select(Citation).where(Citation.course_id == course.course_id)
    ).first()
    return existing is None


def parser_node(state: AgentState) -> AgentState:
    all_citations: list[BookCitation] = []
    errors: list[str] = []

    with SessionLocal() as session:
        courses = session.scalars(select(Course)).all()

    total = len(courses)
    for i, course in enumerate(courses, 1):
        label = f"[{i:>4}/{total}] {course.course_id} {course.course_name} {course.instructor}（{course.semester}）"
        with SessionLocal() as session:
            if not _needs_parsing(session, course):
                print(f"  SKIP  {label}")
                continue
        try:
            citations = _parse_one(course)
            _save_citations(course.course_id, citations)
            print(f"  OK    {label}  → {len(citations)} 筆書目")
            all_citations.extend(citations)
        except Exception as e:
            errors.append(f"[parser] {course.course_id}: {e}")
            print(f"  ERROR {label}  → {e}")
            traceback.print_exc()

    return {"citations": all_citations, "errors": errors}

if __name__ == "__main__":
    import sys
    _TEST_LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None

    all_citations: list[BookCitation] = []
    errors: list[str] = []

    with SessionLocal() as session:
        courses = session.scalars(select(Course)).all()

    if _TEST_LIMIT:
        courses = courses[:_TEST_LIMIT]

    total = len(courses)
    for i, course in enumerate(courses, 1):
        label = f"[{i:>4}/{total}] {course.course_id} {course.course_name} {course.instructor}（{course.semester}）"
        with SessionLocal() as session:
            if not _needs_parsing(session, course):
                print(f"  SKIP  {label}")
                continue
        try:
            citations = _parse_one(course)
            _save_citations(course.course_id, citations)
            print(f"  OK    {label}  → {len(citations)} 筆書目")
            all_citations.extend(citations)
        except Exception as e:
            errors.append(f"[parser] {course.course_id}: {e}")
            print(f"  ERROR {label}  → {e}")
            traceback.print_exc()

    print(f"\n共解析 {len(all_citations)} 筆書目")
    if errors:
        print(f"錯誤 {len(errors)} 筆：")
        for e in errors:
            print(f"  {e}")
