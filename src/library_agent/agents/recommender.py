import math

from sqlalchemy import delete, select

from library_agent.db.models import (
    Citation,
    Course,
    HoldingCheck as HoldingCheckDB,
    Recommendation as RecommendationDB,
    VerifiedBook as VerifiedBookDB,
)
from library_agent.db.session import SessionLocal
from library_agent.state import AgentState, HoldingStatus, PurchasePriority


def _compute(
    status: HoldingStatus,
    is_required: bool,
    enrolled: int,
    current_copies: int,
) -> tuple[PurchasePriority, int, str]:
    """回傳 (priority, suggested_copies, rationale)。"""

    if status == HoldingStatus.OWNED_EBOOK:
        return PurchasePriority.SKIP, 0, "已有電子書，無需採購實體書"

    # 計算這門課需要幾冊：指定用書每 10 人 1 冊，選用書每 20 人 1 冊
    ratio = 10 if is_required else 20
    if enrolled > 0:
        copies_needed = max(1, math.ceil(enrolled / ratio))
    else:
        copies_needed = 2 if is_required else 1

    book_type = "指定用書" if is_required else "選用書"

    if status == HoldingStatus.OWNED_PHYSICAL:
        if current_copies >= copies_needed:   # 實體書館藏充足->跳過
            return (
                PurchasePriority.SKIP,
                0,
                f"{book_type}，選課 {enrolled} 人，館藏 {current_copies} 冊已足夠",
            )
        shortfall = copies_needed - current_copies
        return (
            PurchasePriority.LOW,
            shortfall,
            f"{book_type}，選課 {enrolled} 人，現有 {current_copies} 冊，建議補充 {shortfall} 冊",
        )

    # MISSING 或 PARTIAL
    priority = PurchasePriority.HIGH if is_required else PurchasePriority.MEDIUM
    missing_reason = (
        "書目記錄存在但無實體館藏" if status == HoldingStatus.PARTIAL else "館藏不存在"
    )
    return (
        priority,
        copies_needed,
        f"{book_type}，選課 {enrolled} 人，{missing_reason}，建議採購 {copies_needed} 冊",
    )


def _process_one(
    holding: HoldingCheckDB,
    citation: Citation,
    course: Course,
) -> tuple[PurchasePriority, int, str]:
    status = HoldingStatus(holding.status)
    return _compute(
        status=status,
        is_required=citation.is_required,
        enrolled=course.enrolled_count or 0,
        current_copies=holding.holdings_count,
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
