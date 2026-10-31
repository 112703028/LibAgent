"""人工審核的決定與撤銷（儀表板的審核按鈕與命令列 `main review` 共用這一份邏輯）。

動作語意：
  matched   買配對到的書  → approved；沿用驗證器配到的書與已查好的館藏，沒有採購書名
  original  買原書        → approved；採購書名＝原書名、原作者、（過檢查碼的）原書 ISBN
  custom    輸入書名加入  → approved；採購書名＝使用者輸入的書名，作者留空，ISBN 選填
  none      都不買／捨棄  → rejected
「採購書名」另存在 purchase_*，**不覆蓋** canonical_*（驗證器配到的證據，審核要留著）。

決定只寫資料庫（不 commit，由呼叫端負責），並把 review_applied_at 設成 NULL：
放行的書要等「套用審核結果」重查館藏、重算採購建議後才會出現在採購清單（見 review_apply）。
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select

from library_agent.book_identity import normalize_isbn
from library_agent.db.models import Citation, HoldingCheck, Recommendation, VerifiedBook

ACTIONS = ("matched", "original", "custom", "none")
MAX_IDS = 200
MAX_TITLE = 500


class ReviewError(ValueError):
    """輸入不合法。拋出時不會寫入任何東西。"""


@dataclass(frozen=True)
class DecideResult:
    changed: list    # 這次真的寫入的書目 id
    skipped: list    # 沒寫入的（已被處理、不存在）


@dataclass(frozen=True)
class UndoResult:
    changed: list
    skipped: list


def _validate(vb_ids: list, action: str, title: str | None, isbn: str | None) -> tuple[list, str | None, str | None]:
    if action not in ACTIONS:
        raise ReviewError(f"未知的動作：{action}")
    ids = sorted({int(i) for i in vb_ids})
    if not ids:
        raise ReviewError("沒有指定書目")
    if len(ids) > MAX_IDS:
        raise ReviewError(f"一次最多處理 {MAX_IDS} 筆")
    clean_title = clean_isbn = None
    if action == "custom":
        clean_title = (title or "").strip()
        if not clean_title:
            raise ReviewError("請輸入書名")
        if len(clean_title) > MAX_TITLE:
            raise ReviewError(f"書名最多 {MAX_TITLE} 字")
        if (isbn or "").strip():
            clean_isbn = normalize_isbn(isbn)
            if clean_isbn is None:
                raise ReviewError("ISBN 格式不正確（需為有效的 ISBN-10 或 ISBN-13）")
    return ids, clean_title, clean_isbn


def decide(session, vb_ids: list, action: str, *, title: str | None = None, isbn: str | None = None,
           now: datetime | None = None) -> DecideResult:
    """對一組書目下決定。只處理仍是 pending 的；其餘（別人已處理、不存在）列入 skipped，絕不覆蓋。"""
    ids, clean_title, clean_isbn = _validate(vb_ids, action, title, isbn)
    now = now or datetime.now(timezone.utc)
    # FOR UPDATE：兩個人同時處理同一組時，後到的會等前一個 commit，再依「是否仍是 pending」重新篩選
    rows = session.execute(
        select(VerifiedBook, Citation)
        .join(Citation, Citation.id == VerifiedBook.citation_id)
        .where(VerifiedBook.id.in_(ids), VerifiedBook.review_status == "pending")
        .with_for_update(of=VerifiedBook)
    ).all()

    changed = []
    for vb, citation in rows:
        vb.review_status = "rejected" if action == "none" else "approved"
        vb.review_choice = action
        if action == "original":
            vb.purchase_title = citation.title
            vb.purchase_authors = citation.authors or "[]"
            vb.purchase_isbn = normalize_isbn(citation.isbn)
        elif action == "custom":
            vb.purchase_title, vb.purchase_authors, vb.purchase_isbn = clean_title, "[]", clean_isbn
        else:
            vb.purchase_title = vb.purchase_authors = vb.purchase_isbn = None
        vb.reviewed_at = now
        vb.review_applied_at = None
        changed.append(vb.id)
    session.flush()
    return DecideResult(changed=sorted(changed), skipped=[i for i in ids if i not in changed])


def undo(session, vb_ids: list, *, now: datetime | None = None) -> UndoResult:
    """把有決定的書改回 pending，清掉選擇與採購書名。沒有決定的（含舊的命令列核准）列入 skipped。
    原本是 original／custom 的，一併刪掉那本的館藏列與指向它的採購建議：那是用採購書名查的，
    留著會讓之後改選「買配對到的書」時用到錯的館藏。"""
    ids = sorted({int(i) for i in vb_ids})
    if not ids:
        raise ReviewError("沒有指定書目")
    if len(ids) > MAX_IDS:
        raise ReviewError(f"一次最多處理 {MAX_IDS} 筆")
    rows = session.scalars(
        select(VerifiedBook)
        .where(VerifiedBook.id.in_(ids), VerifiedBook.review_choice.is_not(None))
        .with_for_update()
    ).all()

    changed = []
    for vb in rows:
        if vb.review_choice in ("original", "custom"):
            holding_ids = list(session.scalars(select(HoldingCheck.id).where(HoldingCheck.verified_book_id == vb.id)))
            if holding_ids:
                session.execute(delete(Recommendation).where(Recommendation.holding_id.in_(holding_ids)))
                session.execute(delete(HoldingCheck).where(HoldingCheck.id.in_(holding_ids)))
        vb.review_status = "pending"
        vb.review_choice = None
        vb.purchase_title = vb.purchase_authors = vb.purchase_isbn = None
        vb.reviewed_at = vb.review_applied_at = None
        changed.append(vb.id)
    session.flush()
    return UndoResult(changed=sorted(changed), skipped=[i for i in ids if i not in changed])
