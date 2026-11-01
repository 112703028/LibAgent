"""套用審核結果：把人工決定反映到館藏與採購建議。

審核按鈕只寫資料庫（review_actions）；放行的書要等這裡重查館藏、重算採購建議後才會出現在採購清單：
  1. 對「已核准、尚未套用」的書，有採購書名或還沒有館藏列者，用採購書名／ISBN 重查 Alma
     （librarian.recheck_holdings；館藏有變會連帶刪掉指向舊館藏的建議）
  2. recommender 只重算「包含這些書目」的書（recommend(only_citation_ids)）：不重寫全校的關聯列、
     不重問無關書名的同書判斷；簽章沒變不打 LLM，只有新增或數字變了的書才寫新理由
  3. 只有「確實出現在採購建議裡」且重查沒失敗的書才標 review_applied_at；
     其餘不標，下次可重試，不留半套。recommender 若丟例外，整個流程中止、一本都不標。
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from library_agent import runlog
from library_agent.db.filters import not_superseded
from library_agent.db.models import Citation, HoldingCheck, RecommendationCourse, VerifiedBook
from library_agent.db.session import SessionLocal


@dataclass(frozen=True)
class ApplyResult:
    applied: int      # 這次標為已套用的書目數
    rechecked: int    # 這次重查館藏成功的書目數
    errors: list      # 重查或重算過程中的錯誤訊息


def pending_apply_ids(session) -> list[int]:
    """已核准、有決定紀錄、但決定還沒反映到館藏與採購建議的書目。"""
    return list(session.scalars(
        select(VerifiedBook.id)
        .join(Citation, Citation.id == VerifiedBook.citation_id)
        .where(VerifiedBook.review_status == "approved", VerifiedBook.review_choice.is_not(None),
               VerifiedBook.review_applied_at.is_(None), not_superseded())
        .order_by(VerifiedBook.id)
    ))


def apply_review() -> ApplyResult:
    # 延後 import：recommender 在 import 時會建構 OpenAI client；以模組屬性呼叫，測試才換得掉
    from library_agent.agents import librarian, recommender

    with SessionLocal() as s:
        ids = pending_apply_ids(s)
        if not ids:
            return ApplyResult(0, 0, [])
        has_holding = set(s.scalars(select(HoldingCheck.verified_book_id).where(HoldingCheck.verified_book_id.in_(ids))))
        books = s.execute(
            select(VerifiedBook.id, VerifiedBook.citation_id, VerifiedBook.purchase_title)
            .where(VerifiedBook.id.in_(ids))
        ).all()

    recheck_ids = [vb_id for vb_id, _cit, purchase_title in books if purchase_title is not None or vb_id not in has_holding]
    print(f"待套用 {len(ids)} 本；其中 {len(recheck_ids)} 本要重查館藏")

    token = runlog.bind_node("librarian")
    try:
        rechecked = librarian.recheck_holdings(recheck_ids)
    finally:
        runlog.unbind_node(token)

    token = runlog.bind_node("recommender")
    try:
        # 例外往上丟：此時一本都不標為已套用
        out = recommender.recommend(only_citation_ids={cit for _id, cit, _t in books})
    finally:
        runlog.unbind_node(token)

    errors = list(rechecked.errors) + list(out.get("errors", []))
    failed = set(rechecked.failed)
    with SessionLocal() as s:
        linked = set(s.scalars(
            select(RecommendationCourse.citation_id)
            .where(RecommendationCourse.citation_id.in_([cit for _id, cit, _t in books]))
        ))
        ok = [vb_id for vb_id, cit, _t in books if vb_id not in failed and cit in linked]
        if ok:
            s.execute(update(VerifiedBook).where(VerifiedBook.id.in_(ok), VerifiedBook.review_applied_at.is_(None))
                      .values(review_applied_at=datetime.now(timezone.utc)))
            s.commit()
    print(f"套用完成：{len(ok)} 本已反映到採購建議" + (f"；{len(errors)} 個錯誤" if errors else ""))
    return ApplyResult(applied=len(ok), rechecked=len(recheck_ids) - len(failed), errors=errors)
