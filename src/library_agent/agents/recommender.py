import math

from openai import OpenAI
from sqlalchemy import delete, or_, select, update

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
_client = OpenAI(api_key=_settings.openai_api_key, base_url=_settings.openai_base_url)

_STATUS_DESC = {
    HoldingStatus.OWNED_EBOOK: "已有電子書",
    HoldingStatus.OWNED_PHYSICAL: "已有實體書",
    HoldingStatus.PARTIAL: "書目記錄存在但無實體館藏",
    HoldingStatus.MISSING: "館藏不存在",
}


def _decide(
    status: HoldingStatus,
    is_required: bool,
    enrolled: int,
    current_copies: int,
) -> tuple[PurchasePriority, int]:
    """依規則確定性地算出優先級與建議冊數（可測、有比例、有上限、算缺口）。

    冊數 = clamp(需求 − 現有, 0, 上限)，需求 = ceil(選課人數 / 每本可服務人數)。
    優先級矩陣：
                    館藏不存在      有藏但不足      足夠 / 電子書
      指定 (required)   HIGH          MEDIUM          SKIP
      選用 (optional)   MEDIUM        LOW             SKIP
    """
    if status == HoldingStatus.OWNED_EBOOK:
        return PurchasePriority.SKIP, 0
    per = max(_settings.students_per_copy, 1)
    need = math.ceil(enrolled / per) if enrolled > 0 else 1
    suggested = min(max(need - current_copies, 0), _settings.max_purchase_copies)
    if suggested == 0:  # 現有實體館藏已足夠
        return PurchasePriority.SKIP, 0
    if current_copies <= 0:  # 完全沒有實體（missing / 只有書目記錄）
        return (PurchasePriority.HIGH if is_required else PurchasePriority.MEDIUM), suggested
    return (PurchasePriority.MEDIUM if is_required else PurchasePriority.LOW), suggested


def _llm_rationale(
    title: str,
    course_name: str,
    is_required: bool,
    status: HoldingStatus,
    enrolled: int,
    current_copies: int,
    priority: PurchasePriority,
    copies: int,
) -> str:
    """數字已由 _decide 定案；這裡只請 LLM 寫一句可讀理由，失敗則用模板 fallback。"""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        course_name=course_name,
        book_type="指定用書" if is_required else "選用書",
        status_desc=_STATUS_DESC.get(status, status.value),
        current_copies=current_copies,
        enrolled=enrolled if enrolled > 0 else "不明",
        priority=priority.value,
        copies=copies,
    )
    try:
        response = _client.chat.completions.create(
            model=_settings.llm_model_mini,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        if text:
            return text
    except Exception:
        pass
    # LLM 失敗 → 模板
    if priority == PurchasePriority.SKIP:
        return "館藏已足夠，暫不需採購。"
    who = "指定用書" if is_required else "選用書"
    ppl = enrolled if enrolled > 0 else "不明"
    return f"{who}，選課人數 {ppl}、現有 {current_copies} 冊，建議採購 {copies} 冊。"


def _resolve_enrolled(session, course: Course, cache: dict[str, int]) -> int:
    """每門課只解析一次預收人數（#8/#12）：
    優先用 DB 已存的 Course.enrolled_count，沒有才打一次 HTTP 並寫回持久化。
    單次執行內以 course_id memo，避免同課多本書重複打 HTTP。"""
    cid = course.course_id
    if cid in cache:
        return cache[cid]

    n = course.enrolled_count or 0  # DB 已存（>0）→ 直接用，不打 HTTP
    if n <= 0:
        try:
            raw = fetch_student_number(cid, course.semester)
            n = int(raw) if raw is not None else 0
        except Exception:
            n = 0  # 網路失敗等 → 視為人數不明(0)，不擋住採購建議
        if n > 0:  # 抓到才寫回；0/失敗不寫，下次重跑會再試
            session.execute(
                update(Course).where(Course.course_id == cid).values(enrolled_count=n)
            )
    cache[cid] = n
    return n


def recommender_node(_state: AgentState) -> AgentState:
    errors: list[str] = []

    course_ids = _state.get("course_ids")
    with SessionLocal() as session:
        q = (
            select(HoldingCheckDB, Citation, Course)
            .join(VerifiedBookDB, VerifiedBookDB.id == HoldingCheckDB.verified_book_id)
            .join(Citation, Citation.id == VerifiedBookDB.citation_id)
            .join(Course, Course.course_id == Citation.course_id)
            # 排除人工審核退回的書
            .where(or_(VerifiedBookDB.review_status.is_(None),
                       VerifiedBookDB.review_status != "rejected"))
        )
        if course_ids:
            q = q.where(Citation.course_id.in_(course_ids))
        rows = session.execute(q).all()

        # 冪等：citation_id（不會被下游重寫覆蓋）→ 既有建議的 (priority, suggested_copies)。
        # 不能用 holding_id 比對——librarian 每次都先刪再寫 holding_checks，
        # 就算館藏狀態沒變，holding_id 也會變成新的 auto-increment 值。
        # 數字沒變就代表館藏狀態/選課人數/是否指定書都沒變，不必重打 LLM 重寫理由文字。
        existing = {
            citation_id: (priority, suggested_copies)
            for citation_id, priority, suggested_copies in session.execute(
                select(
                    VerifiedBookDB.citation_id,
                    RecommendationDB.priority,
                    RecommendationDB.suggested_copies,
                )
                .join(HoldingCheckDB, HoldingCheckDB.id == RecommendationDB.holding_id)
                .join(VerifiedBookDB, VerifiedBookDB.id == HoldingCheckDB.verified_book_id)
            ).all()
        }

    total = len(rows)
    counts = {p: 0 for p in PurchasePriority}
    skipped = 0
    enrolled_cache: dict[str, int] = {}  # #12：每門課的人數在單次執行內只解析一次

    with SessionLocal() as session:
        for i, (holding, citation, course) in enumerate(rows, 1):
            try:
                enrolled = _resolve_enrolled(session, course, enrolled_cache)
                status = HoldingStatus(holding.status)
                priority, suggested_copies = _decide(
                    status, citation.is_required, enrolled, holding.holdings_count
                )

                if existing.get(citation.id) == (priority.value, suggested_copies):
                    skipped += 1
                    counts[priority] += 1
                    print(f"  SKIP  [{i:>5}/{total}] {citation.title[:40]}")
                    continue

                rationale = _llm_rationale(
                    citation.title, course.course_name, citation.is_required,
                    status, enrolled, holding.holdings_count, priority, suggested_copies,
                )

                # 先刪再寫（覆蓋數字真的變動的舊建議）
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

    print(f"  SKIP {skipped} 筆（建議未變），重算 {total - skipped} 筆")

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
