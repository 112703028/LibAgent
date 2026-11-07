"""購物車決定與「書」的對應：決定時的快照，以及書的識別鍵改變時的對帳搬移。

購物車的決定以 book_key 對應（purchase_decisions），而 book_key 是從書目資料算出來的：
書商補上 ISBN、審核改書名、重跑 pipeline 都可能讓它改變。決定時另記下這本書的書名與它由哪些課綱書目組成；
課綱書目的 id 不會因為重新驗證而改變，所以是「這是哪本書」的線索。
"""
from sqlalchemy import select

from library_agent.db.models import HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook


def snapshot(session, book_key: str) -> tuple[str | None, list[int]]:
    """這個鍵目前的採購建議的書名（代表書目的採購書名，沒有就用驗證器配到的）與課綱書目 id（去重、遞增）。
    沒有建議時回 (None, [])。"""
    row = session.execute(
        select(Recommendation.id, VerifiedBook.buy_title)
        .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
        .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
        .where(Recommendation.book_key == book_key)
    ).first()
    if row is None:
        return None, []
    rec_id, title = row
    citation_ids = session.scalars(
        select(RecommendationCourse.citation_id).where(RecommendationCourse.recommendation_id == rec_id)
    ).all()
    return title, sorted(set(citation_ids))
