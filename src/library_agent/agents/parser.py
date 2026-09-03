import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
from sqlalchemy import delete, select

from library_agent.config import get_settings
from library_agent.db.models import Citation, Course
from library_agent.db.session import SessionLocal
from library_agent.prompts.parser_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from library_agent.state import AgentState, BookCitation
from library_agent.integrations.nccu_syllabus import fetch_syllabus_pdf

_settings = get_settings()
_client = OpenAI(api_key=_settings.openai_api_key, base_url=_settings.openai_base_url)
_MAX_WORKERS = 6

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
        model=_settings.llm_model_mini,
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


def _process_one(course: Course) -> tuple[str, list[BookCitation] | None, str | None]:
    """跑在 worker 執行緒：只算（打 LLM），不碰 DB。例外用 return 回報，不 raise，
    這樣一門課失敗不會拖垮整個 ThreadPoolExecutor。"""
    try:
        return course.course_id, _parse_one(course), None
    except Exception as e:
        return course.course_id, None, f"[parser] {course.course_id}: {e}"


def _stage_citations(session, course_id: str, citations: list[BookCitation]) -> None:
    """把單筆寫入暫存進「外部傳入的」session（先刪再寫），不 commit。
    由呼叫端（parser_node 主執行緒）統一分批 commit。"""
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


def _save_citations(course_id: str, citations: list[BookCitation]) -> None:
    """獨立 session 版本，供 __main__ 逐筆測試使用。"""
    with SessionLocal() as session:
        _stage_citations(session, course_id, citations)
        session.commit()


_PLACEHOLDER = {"tbd", "n/a", "na", "none", "待補", "待定", ""}

def _needs_parsing(course: Course, parsed_ids: set[str]) -> bool:
    if course.raw_content.strip().lower() in _PLACEHOLDER:
        return False
    return course.course_id not in parsed_ids


def parser_node(state: AgentState) -> AgentState:
    """worker 併發打 LLM（_process_one，只算不碰 DB）；主執行緒用單一 session
    依完成順序收結果、_stage_citations 暫存、每 _BATCH 筆 commit 一次。"""
    errors: list[str] = []
    course_ids = state.get("course_ids")
    _BATCH = 50

    with SessionLocal() as session:
        q = select(Course)
        if course_ids:
            q = q.where(Course.course_id.in_(course_ids))
        courses = session.scalars(q).all()

        # A：一次撈出「已有書目」的 course_id 集合，取代每門課一次的 _needs_parsing 查詢
        parsed_ids = set(session.scalars(select(Citation.course_id).distinct()).all())

    todo = []
    skipped = 0
    for course in courses:
        if _needs_parsing(course, parsed_ids):
            todo.append(course)
        else:
            skipped += 1
    print(f"  SKIP {skipped} 筆（已解析/佔位符），待處理 {len(todo)} 筆")

    total = len(todo)
    done = 0
    written = 0

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor, SessionLocal() as session:
        futures = {executor.submit(_process_one, course): course for course in todo}
        for future in as_completed(futures):
            done += 1
            course = futures[future]
            label = f"[{done:>5}/{total}] {course.course_id} {course.course_name} {course.instructor}（{course.semester}）"
            course_id, citations, error = future.result()

            if error:
                errors.append(error)
                print(f"  ERROR {label}  → {error}")
                continue

            _stage_citations(session, course_id, citations)
            written += 1
            print(f"  OK    {label}  → {len(citations)} 筆書目")
            if written % _BATCH == 0:
                session.commit()

        session.commit()  # 收尾：把最後不足一批的寫入 commit

    return {"errors": errors}

if __name__ == "__main__":
    import sys
    _TEST_LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None

    all_citations: list[BookCitation] = []
    errors: list[str] = []

    with SessionLocal() as session:
        courses = session.scalars(select(Course)).all()
        parsed_ids = set(session.scalars(select(Citation.course_id).distinct()).all())

    if _TEST_LIMIT:
        courses = courses[:_TEST_LIMIT]

    total = len(courses)
    for i, course in enumerate(courses, 1):
        label = f"[{i:>4}/{total}] {course.course_id} {course.course_name} {course.instructor}（{course.semester}）"
        if not _needs_parsing(course, parsed_ids):
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
