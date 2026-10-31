"""待審書目的分類與分組（純函式，不碰資料庫）。

待審核（review_status='pending'）的書有三種來源，後果各不相同，所以分開處理：
  mismatch  match_auditor 判為配錯：已被 purchasable() 擋在採購清單之外
  notfound  validator 在權威書目查無此書（verified=False）：仍在採購清單裡，只是未驗證
  other     其餘（例如判同一本但仍待審）

同一本書常被多門課重複列出，審核人員要做的決定是「一本書一次」，所以合併成組：
  mismatch  依（原書名, 配到的書名）。配到的書名必須在鍵裡：同名不同作者的書會配到不同本，絕不能併成一組
  notfound  只依原書名：查無此書時 matched_title 只是原書名的占位
  other     每筆自成一組
書名一律用 match_auditor 的 _norm 正規化（大小寫、全半形、空白、簡繁）。
"""
from collections import defaultdict
from dataclasses import dataclass

from library_agent.agents.match_auditor import _norm

KINDS = ("mismatch", "notfound", "other")

# 優先級由嚴重到輕微；不在任何採購建議裡的組排最後
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2, "skip": 3}
_NOT_IN_LIST = 9


@dataclass(frozen=True)
class PendingItem:
    """一筆待審書目（一個 citation 與它的驗證結果）。"""
    vb_id: int
    citation_id: int
    course_id: str
    course_name: str
    enrolled: int
    title: str
    authors: list
    matched_title: str
    matched_authors: list
    source: str | None
    verified: bool
    verdict: str | None
    judge: str | None
    reason: str | None
    confidence: float


@dataclass(frozen=True)
class ReviewGroup:
    """一組＝一本書（可能被多門課用到）。"""
    kind: str
    title: str
    authors: list
    matched_title: str
    matched_authors: list
    source: str | None
    judge: str | None
    reason: str | None
    low_confidence: bool
    courses: list                 # [(課名, 人數)]，同一門課只列一次，依課名排序
    items: int                    # 組內書目數
    enrolled_total: int           # 各不同課程人數加總
    vb_ids: tuple                 # 組內所有書目（verified_book）的 id，由小到大
    priority: str | None          # 所在採購建議的優先級（組內最嚴重者）；不在清單為 None
    copies: int | None


def classify(item: PendingItem) -> str:
    if item.verdict == "mismatch":
        return "mismatch"
    return "other" if item.verified else "notfound"


def _key(kind: str, item: PendingItem) -> tuple:
    if kind == "mismatch":
        return (kind, _norm(item.title), _norm(item.matched_title))
    if kind == "notfound":
        return (kind, _norm(item.title))
    return (kind, item.vb_id)


def _make_group(kind: str, members: list, rec_of: dict, threshold: float) -> ReviewGroup:
    members = sorted(members, key=lambda m: m.vb_id)
    rep = members[0]
    per_course: dict = {}
    for m in members:
        per_course.setdefault(m.course_id, (m.course_name, m.enrolled))   # 同課只算一次
    in_list = [rec_of[m.citation_id] for m in members if m.citation_id in rec_of]
    priority, copies = (min(in_list, key=lambda pc: _PRIORITY_RANK.get(pc[0], _NOT_IN_LIST))
                        if in_list else (None, None))
    return ReviewGroup(
        kind=kind, title=rep.title, authors=rep.authors, matched_title=rep.matched_title,
        matched_authors=rep.matched_authors, source=rep.source, judge=rep.judge, reason=rep.reason,
        low_confidence=any(m.confidence < threshold for m in members),
        courses=sorted(per_course.values()), items=len(members),
        enrolled_total=sum(n for _, n in per_course.values()),
        vb_ids=tuple(m.vb_id for m in members), priority=priority, copies=copies,
    )


def _sort_key(kind: str):
    if kind == "notfound":   # 先處理採購清單裡最重要的，再看影響人數
        return lambda g: (_PRIORITY_RANK.get(g.priority, _NOT_IN_LIST), -g.enrolled_total, g.title)
    return lambda g: (-g.enrolled_total, g.title)


def group_pending(items: list, rec_of: dict, threshold: float) -> dict:
    """rec_of：citation_id → (優先級, 冊數)，只含可採購的建議。回傳每個 kind 一個已排序的清單（三個鍵都在）。"""
    buckets: dict = defaultdict(list)
    for item in items:
        buckets[_key(classify(item), item)].append(item)
    out: dict = {kind: [] for kind in KINDS}
    for key, members in buckets.items():
        out[key[0]].append(_make_group(key[0], members, rec_of, threshold))
    for kind in KINDS:
        out[kind].sort(key=_sort_key(kind))
    return out
