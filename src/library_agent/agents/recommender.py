import json

from openai import OpenAI
from sqlalchemy import delete, select

from library_agent.config import get_settings
from library_agent.db.models import (
    Citation,
    Course,
    HoldingCheck as HoldingCheckDB,
    Recommendation as RecommendationDB,
    VerifiedBook as VerifiedBookDB,
)
from library_agent.db.session import SessionLocal
from library_agent.integrations.nccu_syllabus import fetch_student_number
from library_agent.prompts.recommender_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from library_agent.state import AgentState, HoldingStatus, PurchasePriority

_settings = get_settings()
_client = OpenAI(api_key=_settings.openai_api_key)

_STATUS_DESC = {
    HoldingStatus.OWNED_EBOOK: "已有電子書",
    HoldingStatus.OWNED_PHYSICAL: "已有實體書",
    HoldingStatus.PARTIAL: "書目記錄存在但無實體館藏",
    HoldingStatus.MISSING: "館藏不存在",
}


def _llm_compute(
    status: HoldingStatus,
    is_required: bool,
    enrolled: int,
    current_copies: int,
    title: str,
    course_name: str,
) -> tuple[PurchasePriority, int, str]:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        course_name=course_name,
        book_type="指定用書" if is_required else "選用書",
        is_required_str="必讀" if is_required else "選讀",
        status_desc=_STATUS_DESC.get(status, status.value),
        current_copies=current_copies,
        enrolled=enrolled if enrolled > 0 else "不明",
    )
    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    priority = PurchasePriority(data["priority"].lower())
    suggested_copies = int(data["suggested_copies"])
    rationale = str(data["rationale"])
    return priority, suggested_copies, rationale


def _process_one(
    holding: HoldingCheckDB,
    citation: Citation,
    course: Course,
) -> tuple[PurchasePriority, int, str]:
    status = HoldingStatus(holding.status)
    raw = fetch_student_number(course.course_id, course.semester)
    try:
        enrolled = int(raw) if raw is not None else 0
    except (ValueError, TypeError):
        enrolled = 0

    return _llm_compute(
        status=status,
        is_required=citation.is_required,
        enrolled=enrolled,
        current_copies=holding.holdings_count,
        title=citation.title,
        course_name=course.course_name,
    )


def recommender_node(_state: AgentState) -> AgentState:
    errors: list[str] = []

    with SessionLocal() as session:
        rows = session.execute(
            select(HoldingCheckDB, Citation, Course)
            .join(VerifiedBookDB, VerifiedBookDB.id == HoldingCheckDB.verified_book_id)
            .join(Citation, Citation.id == VerifiedBookDB.citation_id)
            .join(Course, Course.course_id == Citation.course_id)
        ).all()

    total = len(rows)
    counts = {p: 0 for p in PurchasePriority}

    with SessionLocal() as session:
        for i, (holding, citation, course) in enumerate(rows, 1):
            try:
                priority, suggested_copies, rationale = _process_one(
                    holding, citation, course
                )

                # 冪等：先刪再寫
                session.execute(
                    delete(RecommendationDB).where(
                        RecommendationDB.holding_id == holding.id
                    )
                )
                session.add(RecommendationDB(
                    course_id=course.course_id,
                    holding_id=holding.id,
                    priority=priority.value,
                    suggested_copies=suggested_copies,
                    rationale=rationale,
                ))
                counts[priority] += 1

                print(
                    f"  [{i:>5}/{total}] [{priority.value:<8}] "
                    f"{citation.title[:40]:<40}  {rationale}"
                )

                if i % 500 == 0:
                    session.commit()
                    print(f"  --- committed {i}/{total} ---")

            except Exception as e:
                errors.append(f"[recommender] holding {holding.id}: {e}")
                print(f"  [{i:>5}/{total}] [ERROR   ] {citation.title[:40]}  → {e}")

        session.commit()

    print(f"\n採購建議統計：")
    for priority, count in counts.items():
        if count:
            print(f"  {priority.value:<10} {count} 筆")

    return {"errors": errors}


if __name__ == "__main__":
    result = recommender_node({})
    if result["errors"]:
        print(f"\n錯誤 {len(result['errors'])} 筆：")
        for e in result["errors"]:
            print(f"  {e}")
