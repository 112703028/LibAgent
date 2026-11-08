"""購物車決定與「書」的對應：決定時的快照，以及書的識別鍵改變時的對帳搬移。

購物車的決定以 book_key 對應（purchase_decisions），而 book_key 是從書目資料算出來的：
書商補上 ISBN、審核改書名、重跑 pipeline 都可能讓它改變。決定時另記下這本書的書名與它由哪些課綱書目組成；
課綱書目的 id 不會因為重新驗證而改變，所以是「這是哪本書」的線索。
"""
import json
from collections import defaultdict
from typing import NamedTuple

from sqlalchemy import select

from library_agent.db.filters import not_superseded, purchasable
from library_agent.db.models import (
    Citation, HoldingCheck, PurchaseDecision, Recommendation, RecommendationCourse, VerifiedBook,
)


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


class ReconcileResult(NamedTuple):
    refreshed: int   # 鍵還在、快照有更新
    moved: int       # 鍵變了、搬到新鍵
    orphaned: int    # 鍵不在、也搬不了（成為「找不到的決定」）


def reconcile(session) -> ReconcileResult:
    """重算採購建議之後對帳（由呼叫端 commit）。

    - 鍵還在 → 快照更新成這本書現在的書名與課綱書目（之後又多了課，下次搬移的線索才完整）。
    - 鍵不在 → 決定記下的課綱書目中，仍屬於某筆建議者全部指向同一本、且那本還沒有決定 → 搬過去：
      只改 book_key 與快照，冊數、決定當時的建議、決定時間都不動。
    - 其他（新書已有決定、拆成兩本以上、書不見了、沒有快照）一律不動，成為「找不到的決定」。
    決定時間新的先處理，所以兩筆舊決定併成同一本時，搬過去的是較新的那筆。
    """
    rows = session.execute(
        select(Recommendation.id, Recommendation.book_key, VerifiedBook.buy_title)
        .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
        .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
        .where(Recommendation.book_key.is_not(None))
    ).all()
    key_of_rec = {rec_id: key for rec_id, key, _t in rows}
    title_of = {key: title for _r, key, title in rows}
    cits_of: dict[str, set[int]] = defaultdict(set)
    key_of_cit: dict[int, str] = {}
    for rec_id, cit in session.execute(select(RecommendationCourse.recommendation_id, RecommendationCourse.citation_id)):
        key = key_of_rec.get(rec_id)
        if key is not None:
            cits_of[key].add(cit)
            key_of_cit[cit] = key

    decisions = session.scalars(
        select(PurchaseDecision).order_by(PurchaseDecision.decided_at.desc(), PurchaseDecision.id.desc())
    ).all()
    taken = {d.book_key for d in decisions if d.book_key in title_of}

    def snap(d, key: str) -> bool:
        title, cids = title_of[key], json.dumps(sorted(cits_of[key]))
        changed = (d.book_key, d.title, d.citation_ids) != (key, title, cids)
        d.book_key, d.title, d.citation_ids = key, title, cids
        return changed

    refreshed = moved = orphaned = 0
    for d in decisions:
        if d.book_key in title_of:
            refreshed += snap(d, d.book_key)
    for d in decisions:
        if d.book_key in title_of:
            continue
        ids = json.loads(d.citation_ids) if d.citation_ids else []
        targets = {key_of_cit[c] for c in ids if c in key_of_cit}
        if len(targets) == 1 and (target := next(iter(targets))) not in taken:
            snap(d, target)
            taken.add(target)
            moved += 1
        else:
            orphaned += 1
    session.flush()
    return ReconcileResult(refreshed, moved, orphaned)


def orphans(session) -> list[dict]:
    """找不到對應的書的決定：冊數 ≥ 1、鍵不在任何可採購的採購建議裡（與購物車同一個條件）。
    決定 0（這次不買）不列；依決定時間新到舊。"""
    live = set(session.scalars(
        select(Recommendation.book_key)
        .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
        .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
        .join(Citation, Citation.id == VerifiedBook.citation_id)
        .where(Recommendation.book_key.is_not(None), purchasable(), not_superseded())
    ).all())
    rows = session.scalars(
        select(PurchaseDecision).where(PurchaseDecision.copies >= 1)
        .order_by(PurchaseDecision.decided_at.desc(), PurchaseDecision.id.desc())
    ).all()
    return [{"book_key": d.book_key, "title": d.title, "copies": d.copies} for d in rows if d.book_key not in live]
