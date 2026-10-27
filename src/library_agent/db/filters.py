"""下游共用的查詢條件。

採購建議、報表、dashboard 必須用同一組條件判斷「這本書可以進採購決策嗎」，
否則同一本書會在不同介面出現不一致的結果。
"""
from sqlalchemy import and_, or_

from library_agent.db.models import Citation, VerifiedBook


def not_rejected():
    """排除人工審核退回的書。"""
    return or_(VerifiedBook.review_status.is_(None), VerifiedBook.review_status != "rejected")


def pairing_trusted():
    """排除 match_auditor 判定「配到的記錄不是原書」且尚未經人工放行的書。

    配錯的書若只標記待審卻仍流進下游，等於沒有攔到——會以錯書的館藏狀態產生採購建議。
    因此這裡預設擋住，只有人工明確 approved 才放行（人看過後認定其實是同一本）。
    pairing_verdict 為 NULL（舊資料、還沒稽核）視為通過，以免稽核尚未補跑就讓報表整批消失。
    """
    return or_(
        VerifiedBook.pairing_verdict.is_(None),
        VerifiedBook.pairing_verdict != "mismatch",
        VerifiedBook.review_status == "approved",
    )


def not_superseded():
    """排除被回饋迴路換掉的舊 citation（superseded_by_iteration 非空）。"""
    return Citation.superseded_by_iteration.is_(None)


def purchasable():
    """可以進採購決策的書：沒被退回、配對可信。"""
    return and_(not_rejected(), pairing_trusted())
