import json
import traceback

from openai import OpenAI
from sqlalchemy import select

from library_agent.config import get_settings
from library_agent.db.models import Citation, Course
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.integrations.nccu_syllabus import fetch_syllabus, fetch_student_number
from library_agent.prompts.discoverer_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from library_agent.progress import report
from library_agent.state import AgentState, BookCitation

_settings = get_settings()
_client = OpenAI(api_key=_settings.openai_api_key, base_url=_settings.openai_base_url)

_AI_CONFIDENCE_CAP = 0.75


def _existing_titles(course_id: str) -> list[str]:
    with SessionLocal() as session:
        return list(session.scalars(
            select(Citation.title).where(Citation.course_id == course_id)
        ).all())


def _needs_discovery(course_id: str, has_citations: set[str]) -> bool:
    """只處理 parser 完全沒抓到書目的課程。"""
    return course_id not in has_citations


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


def _build_rediscover_prompt(course_name: str, syllabus_text: str, failed_titles: list[str]) -> str:
    failed_str = "\n".join(f"- {t}" for t in failed_titles)
    base = USER_PROMPT_TEMPLATE.format(
        course_name=course_name,
        syllabus_text=syllabus_text[:3000],
        existing_titles="（無）",
    )
    return base + (
        "\n\n以下推薦書經查證權威書目資料庫後不存在，請勿再推薦這些書名，"
        "改推薦其他真實存在的替代書目：\n" + failed_str
    )


def _failed_ai_citations(session, course_id: str) -> list[Citation]:
    """該課「[AI推薦] 且驗證失敗（not_found/ncl_not_found）且未被 superseded」的 citation。
    排除 quota_exceeded（配額用完≠書不存在）。"""
    rows = session.execute(
        select(Citation)
        .join(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
        .where(
            Citation.course_id == course_id,
            Citation.superseded_by_iteration.is_(None),
            Citation.raw_mention.like("[AI推薦%"),
            VerifiedBookDB.source.in_(("not_found", "ncl_not_found")),
        )
    ).scalars().all()
    return list(rows)


def _rediscover_one(course: Course, failed_titles: list[str], syllabus_text: str) -> list[BookCitation]:
    if not syllabus_text.strip():
        return []
    user_prompt = _build_rediscover_prompt(course.course_name, syllabus_text, failed_titles)
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


def _save_citations(citations: list[BookCitation], iteration: int = 0) -> None:
    with SessionLocal() as session:
        for c in citations:
            raw = c.raw_mention
            if iteration >= 1 and raw.startswith("[AI推薦]"):
                raw = raw.replace("[AI推薦]", f"[AI推薦-第{iteration}輪]", 1)
            session.add(Citation(
                course_id=c.course_id,
                title=c.title,
                authors=json.dumps(c.authors, ensure_ascii=False),
                edition=c.edition,
                isbn=c.isbn,
                publisher=c.publisher,
                year=c.year,
                is_required=c.is_required,
                raw_mention=raw,
                confidence=c.confidence,
            ))
        session.commit()


def discoverer_node(state: AgentState) -> AgentState:
    errors: list[str] = []
    course_ids = state.get("course_ids")
    iteration = state.get("discovery_iteration", 0)

    # 重推模式（迴路第 N 輪）：只處理有 AI 推薦書驗證失敗的課
    if iteration >= 1:
        with SessionLocal() as session:
            q = select(Course)
            if course_ids:
                q = q.where(Course.course_id.in_(course_ids))
            courses = session.scalars(q).all()

            for i, course in enumerate(courses, 1):
                report(i, len(courses), course.course_name, "門課")  # 頂端：沒有失敗書的課會 continue
                failed = _failed_ai_citations(session, course.course_id)
                if not failed:
                    continue
                failed_titles = [c.title for c in failed]
                print(f"[重推] {course.course_id} {course.course_name} 第{iteration}輪："
                      f"失敗書名={failed_titles}", flush=True)
                try:
                    syllabus_text = fetch_syllabus(course.course_id, course.semester) or ""
                    new_books = _rediscover_one(course, failed_titles, syllabus_text)
                    # 舊失敗書標記 superseded（保留、不刪）
                    for c in failed:
                        c.superseded_by_iteration = iteration
                    session.commit()
                    if new_books:
                        _save_citations(new_books, iteration)
                        print(f"[重推] {course.course_id} 第{iteration}輪 → 新推薦="
                              f"{[b.title for b in new_books]}", flush=True)
                    else:
                        print(f"[重推] {course.course_id} 第{iteration}輪 → 無替代書", flush=True)
                except Exception as e:
                    errors.append(f"[discoverer-rediscover] {course.course_id}: {e}")
                    print(f"  ERROR 重推 {course.course_id}: {e}", flush=True)
        return {"errors": errors, "discovery_iteration": iteration}

    # 初次推薦模式（現有邏輯，原封不動）
    with SessionLocal() as session:
        q = select(Course)
        if course_ids:
            q = q.where(Course.course_id.in_(course_ids))
        courses = session.scalars(q).all()

        # A：一次撈出「已有書目」的 course_id 集合，取代每門課一次的 _needs_discovery 查詢
        has_citations = set(session.scalars(select(Citation.course_id).distinct()).all())

    total = len(courses)
    for i, course in enumerate(courses, 1):
        label = f"[{i:>4}/{total}] {course.course_id} {course.course_name}"
        report(i, total, course.course_name, "門課")  # 放在頂端：下面的 SKIP 會 continue

        # 如果課程已經有推薦書目就跳過
        if not _needs_discovery(course.course_id, has_citations):
            print(f"  SKIP  {label}")
            continue
        try:
            citations = _discover_one(course)
            if citations:
                _save_citations(citations)
                print(f"  OK    {label}  → {len(citations)} 筆推薦書目")
            else:
                print(f"  EMPTY {label}  （課綱無足夠內容）")
        except Exception as e:
            errors.append(f"[discoverer] {course.course_id}: {e}")
            print(f"  ERROR {label}  → {e}")
            traceback.print_exc()

    return {"errors": errors}


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
