"""採購決策儀表板（唯讀）。

FastAPI 服務，讀既有 DB 呈現缺口分析：KPI 概覽、採購優先級與館藏狀態分布、
可篩選的採購建議明細、待人工審核清單。啟動：`library-agent dashboard`。

圖表用內嵌 HTML/CSS bar（無外部 CDN/JS 依賴、離線可用），配色取自 dataviz
技能的 status palette，並以文字標籤 + 數值確保辨識不依賴顏色。
"""
import csv
import html
import io
import json
import threading
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from fastapi import Body, FastAPI, File, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from library_agent import runlog
from library_agent.book_kind import book_type_key, is_ai_recommended
from library_agent.agents.crawler import DATA_DIR
from library_agent.dept_map import DEPARTMENT_MAP
from library_agent.db.filters import not_superseded, purchasable
from library_agent.db.models import (
    Citation,
    Course,
    HoldingCheck,
    PurchaseDecision,
    Recommendation,
    RecommendationCourse,
    VerifiedBook,
)
from library_agent.db.session import SessionLocal
from library_agent import demand, review_actions
from library_agent.review_apply import pending_apply_ids
from library_agent.review_groups import KINDS, PendingItem, group_pending
from library_agent.trace_view import _authors   # 作者欄位是 JSON 字串，與追溯畫面同一個解析（壞掉回 []）

app = FastAPI(title="Library Agent 採購決策儀表板")

# (key, 顯示名稱, 顏色)  顏色取自 dataviz status palette
_PRIORITY_META = [
    ("high", "HIGH 高", "#d03b3b"),
    ("medium", "MEDIUM 中", "#fab219"),
    ("low", "LOW 低", "#0ca30c"),
    ("skip", "SKIP 略過", "#898781"),
]
# 待審核不是 Recommendation.priority 的列舉值（來自 VerifiedBook.review_status），
# 獨立於 _PRIORITY_META 之外，只在畫長條圖/合併建議表時額外併入。
_PENDING_LABEL = "待審核"
_PENDING_COLOR = "#e0900a"  # 與 KPI 卡片的 warning 色一致
_STATUS_META = [
    ("missing", "缺藏", "#d03b3b"),
    ("partial", "記錄異常", "#fab219"),
    ("owned_physical", "已有實體", "#0ca30c"),
    ("owned_ebook", "已有電子", "#2a78d6"),
    ("unknown", "未知", "#898781"),
]
_PRIORITY_COLOR = {k: c for k, _, c in _PRIORITY_META}
_PRIORITY_LABEL = {k: n for k, n, _ in _PRIORITY_META}
_PRIORITY_RANK = {k: i for i, (k, _, _) in enumerate(_PRIORITY_META)}
_STATUS_COLOR = {k: c for k, _, c in _STATUS_META}
_STATUS_LABEL = {k: n for k, n, _ in _STATUS_META}

def _not_superseded():
    """排除被回饋迴路換掉的舊 citation（superseded_by_iteration 非空）。"""
    return not_superseded()


class CourseRef(NamedTuple):
    """一本書被某門課用到。key = required / optional / ai（該課自己的類別）；
    in_scope = 這門課屬於目前選的學系（沒有篩選時一律 False）。
    sections = 同名的班數：真實資料裡《大學英文（一）》有 42 個班、《經濟學》15 個班，同名的班合成一項顯示，
    但必須標出班數，否則「這本書被 42 個班用到」在畫面上完全看不出來。"""
    name: str
    key: str
    in_scope: bool
    sections: int = 1


class RecRow(NamedTuple):
    """採購建議明細的一列 = 一本書（可能被多門課用到）。冊數是全校合計。"""
    priority: str
    courses: list
    title: str
    status: str
    copies: int
    rationale: str
    rec_id: int = 0      # 展開追溯畫面用（GET /trace/{rec_id}）；放最後，既有建構呼叫不受影響
    unverified: bool = False   # 這本書有書目在驗證階段查無此書（館藏是用書名直接查的結果）
    book_key: str | None = None    # 同一本書的識別；人的決定以它對應（舊格式的建議沒有，也就不能下決定）
    need: int = 0                  # 不設上限的需求冊數（demand.uncapped_copies）；copies 是套上限後的系統建議
    decided: int | None = None     # 人決定的冊數；None＝沒決定（0＝這次不買）
    stale_from: int | None = None  # 決定之後系統建議變了：決定當時的系統建議

    @property
    def final(self) -> int:
        """最終採購冊數＝購物車裡的冊數：有決定就用決定，**沒決定是 0**（系統建議只是參考，沒加入就不算要買）。"""
        return 0 if self.decided is None else self.decided

    @property
    def in_cart(self) -> bool:
        """在購物車裡：明確決定了大於 0 的冊數。決定 0＝明確排除，沒決定＝還沒決定，兩者都不在購物車。"""
        return self.decided is not None and self.decided > 0


# 同一門課有兩個書目時（實測 8 組），取較「重」的類別
_KEY_RANK = {"required": 0, "ai": 1, "optional": 2}


def _confirmed():
    """這個書目有人（或驗證器）確認過：驗證階段查到了，或人工核准了。「未驗證」標記只給兩者皆非的書。"""
    return case((or_(VerifiedBook.verified.is_(True), VerifiedBook.review_status == "approved"), True), else_=False)


def _load_cart(s) -> list[dict]:
    """整個購物車（全校，不分學系）：決定冊數 ≥ 1 且這本書現在還在採購建議裡（與採購表同一個 purchasable() 條件）。
    決定以 book_key 對應；對不上任何採購建議的決定（建議後來被重建掉）不列也不算，與採購表一致。
    排序：優先級高的在前，同優先級依書名、再依 book_key（穩定）。"""
    rows = s.execute(
        select(PurchaseDecision.book_key, VerifiedBook.buy_title, PurchaseDecision.copies,
               Recommendation.suggested_copies, Recommendation.priority, HoldingCheck.status)
        .join(Recommendation, Recommendation.book_key == PurchaseDecision.book_key)
        .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
        .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
        .join(Citation, Citation.id == VerifiedBook.citation_id)
        .where(PurchaseDecision.copies > 0, purchasable(), _not_superseded())
    ).all()
    items = [{"book_key": key, "title": title or "", "copies": copies, "suggested": suggested,
              "priority": priority, "status": status}
             for key, title, copies, suggested, priority, status in rows]
    items.sort(key=lambda i: (_PRIORITY_RANK.get(i["priority"], 9), i["title"], i["book_key"]))
    return items


def _load_data(departments: list[str] | None = None) -> dict:
    """departments：系所名清單（UI 上的名稱）。None/空＝不篩，與原本行為完全相同。
    有值時，KPI、兩張圖、採購建議、待審核明細全部只算這些學系的課；
    Course.department 為 NULL 的課只在不篩時出現。
    採購建議是「一本書一筆」：只要這本書有任一門課屬於所選學系就會顯示，冊數是全校合計。"""
    raw = _expand_departments(departments)

    def scoped(q, *, join_course: bool = True):
        """把查詢限縮到所選學系。q 已經 join 過 Course 的（待審核）傳 join_course=False，
        其餘的要補一次 join；統一用這個函式，才不會漏掉某一個計數。"""
        if raw is None:
            return q
        if join_course:
            q = q.join(Course, Course.course_id == Citation.course_id)
        return q.where(Course.department.in_(raw))

    def rec_scoped(q):
        """限縮到「至少有一門課屬於所選學系」的建議。子查詢的 courses 必須用別名：
        外層查詢已經 join 了「代表課程」的 courses，不用別名的話 SQLAlchemy 會把兩者自動關聯成
        同一張表，變成只檢查代表課程的學系。"""
        if raw is None:
            return q
        dept_course = aliased(Course)
        in_dept = (
            select(RecommendationCourse.id)
            .join(dept_course, dept_course.course_id == RecommendationCourse.course_id)
            .where(RecommendationCourse.recommendation_id == Recommendation.id,
                   dept_course.department.in_(raw))
            .exists()
        )
        return q.where(in_dept)

    with SessionLocal() as s:
        kpis = {
            "courses": s.scalar(scoped(select(func.count()).select_from(Course), join_course=False)) or 0,
            "citations": s.scalar(scoped(select(func.count()).select_from(Citation))) or 0,
            "verified": s.scalar(
                scoped(
                    select(func.count()).select_from(VerifiedBook)
                    .join(Citation, Citation.id == VerifiedBook.citation_id)
                    .where(VerifiedBook.verified.is_(True), _not_superseded())
                )
            ) or 0,
            "pending": s.scalar(
                scoped(
                    select(func.count()).select_from(VerifiedBook)
                    .join(Citation, Citation.id == VerifiedBook.citation_id)
                    .where(VerifiedBook.review_status == "pending", _not_superseded())
                )
            ) or 0,
        }

        # 優先級圖、館藏狀態圖、明細都從 recommendations 出發、數「書」，所以三者的底一致
        prio = {k: 0 for k, _, _ in _PRIORITY_META}
        copies = 0
        for priority, cnt, csum in s.execute(
            rec_scoped(
                select(Recommendation.priority, func.count(), func.sum(Recommendation.suggested_copies))
                .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
                .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
                .join(Citation, Citation.id == VerifiedBook.citation_id)
                .where(purchasable(), _not_superseded())
            ).group_by(Recommendation.priority)
        ).all():
            prio[priority] = cnt
            if priority != "skip":
                copies += int(csum or 0)
        kpis["copies"] = copies

        status = {k: 0 for k, _, _ in _STATUS_META}
        for st, cnt in s.execute(
            rec_scoped(
                select(HoldingCheck.status, func.count())
                .join(Recommendation, Recommendation.holding_id == HoldingCheck.id)
                .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
                .join(Citation, Citation.id == VerifiedBook.citation_id)
                .where(purchasable(), _not_superseded())  # 與優先級圖同一個「可採購」條件
            ).group_by(HoldingCheck.status)
        ).all():
            status[st] = status.get(st, 0) + cnt

        rec_rows = s.execute(
            rec_scoped(
                select(
                    Recommendation.id, Recommendation.priority, VerifiedBook.buy_title,
                    HoldingCheck.status, Recommendation.suggested_copies, Recommendation.rationale,
                    Course.course_name, Course.department, Citation.is_required, Citation.raw_mention,
                    Recommendation.course_id, _confirmed(),
                    Recommendation.book_key, HoldingCheck.holdings_count, Course.enrolled_count,
                )
                .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
                .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
                .join(Citation, Citation.id == VerifiedBook.citation_id)
                .join(Course, Course.course_id == Recommendation.course_id)   # 代表課程：沒有關聯列時的備援
                .where(purchasable(), _not_superseded())
            )
        ).all()

        # 這本書被哪些課用到（關聯表）。沒有關聯列的建議（recommender 重建前的舊格式列）退回用代表課程。
        used_by: dict[int, list[tuple]] = defaultdict(list)
        if rec_rows:
            for rec_id, course_id, name, dept, is_required, raw_mention, confirmed, enrolled in s.execute(
                select(RecommendationCourse.recommendation_id, RecommendationCourse.course_id,
                       Course.course_name, Course.department,
                       RecommendationCourse.is_required, Citation.raw_mention, _confirmed(),
                       RecommendationCourse.enrolled)
                .join(Course, Course.course_id == RecommendationCourse.course_id)
                .join(Citation, Citation.id == RecommendationCourse.citation_id)
                .join(VerifiedBook, VerifiedBook.citation_id == RecommendationCourse.citation_id)
                .where(RecommendationCourse.recommendation_id.in_([r[0] for r in rec_rows]))
            ).all():
                used_by[rec_id].append((course_id, name, dept, is_required, raw_mention, confirmed, enrolled))

        # 人對採購冊數的決定（以 book_key 對應；recommendations 重建後仍在）
        decisions = {
            key: (copies_n, suggested_at)
            for key, copies_n, suggested_at in s.execute(
                select(PurchaseDecision.book_key, PurchaseDecision.copies, PurchaseDecision.suggested_at_decision)
            ).all()
        }

        recs: list[RecRow] = []
        for (rec_id, priority, title, st, rec_copies, rationale,
             rep_name, rep_dept, rep_req, rep_mention, rep_course_id, rep_confirmed,
             book_key, rep_holdings, rep_enrolled) in rec_rows:
            refs: dict[str, CourseRef] = {}
            section_ids: dict[str, set[str]] = defaultdict(set)    # 課名 → 這個名稱下有哪幾個班
            entries = used_by.get(rec_id) or [
                (rep_course_id, rep_name, rep_dept, rep_req, rep_mention, rep_confirmed, rep_enrolled or 0)]
            for course_id, name, dept, is_required, raw_mention, _confirmed_flag, _enrolled in entries:
                key = _book_type_key(is_required, raw_mention)
                prev = refs.get(name)
                section_ids[name].add(course_id)
                refs[name] = CourseRef(
                    name,
                    key if prev is None or _KEY_RANK[key] < _KEY_RANK[prev.key] else prev.key,
                    (raw is not None and dept in raw) or (prev is not None and prev.in_scope),
                )
            courses = [c._replace(sections=len(section_ids[c.name]))
                       for c in sorted(refs.values(), key=lambda c: c.name)]
            need = demand.uncapped_copies(demand.status_of(st), demand.total_enrolled((e[0], e[6]) for e in entries),
                                          rep_holdings or 0)
            decision = decisions.get(book_key) if book_key else None
            recs.append(RecRow(priority, courses, title, st, rec_copies, rationale or "", rec_id,
                               unverified=any(not e[5] for e in entries), book_key=book_key, need=need,
                               decided=None if decision is None else decision[0],
                               stale_from=decision[1] if decision and decision[1] != rec_copies else None))
        recs.sort(key=lambda r: (_PRIORITY_RANK.get(r.priority, 9),
                                 r.courses[0].name if r.courses else "", r.title))

        pending = s.execute(
            scoped(
                select(Course.course_name, Citation.title, Citation.confidence, VerifiedBook.source,
                       Citation.is_required, Citation.raw_mention, VerifiedBook.id)
                .join(Citation, Citation.id == VerifiedBook.citation_id)
                .join(Course, Course.course_id == Citation.course_id)
                .where(VerifiedBook.review_status == "pending", _not_superseded())
                .order_by(Course.course_name),
                join_course=False,
            )
        ).all()

        # 人工審核區塊：待審書目分類並合併成「一組一本書」。學系篩選只影響組內計入哪些書目；
        # 「所在採購建議」是全校的（與冊數一致），所以 rec_of 不套學系篩選。
        rec_of = {
            cit_id: (priority, copies_n)
            for cit_id, priority, copies_n in s.execute(
                select(RecommendationCourse.citation_id, Recommendation.priority, Recommendation.suggested_copies)
                .join(Recommendation, Recommendation.id == RecommendationCourse.recommendation_id)
                .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
                .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
                .join(Citation, Citation.id == VerifiedBook.citation_id)
                .where(purchasable(), _not_superseded())
            ).all()
        }

        def review_items(*conditions) -> list:
            rows = s.execute(
                scoped(
                    select(VerifiedBook.id, Citation.id, Course.course_id, Course.course_name, Course.enrolled_count,
                           Citation.title, Citation.authors, VerifiedBook.canonical_title, VerifiedBook.canonical_authors,
                           VerifiedBook.source, VerifiedBook.verified, VerifiedBook.pairing_verdict,
                           VerifiedBook.pairing_judge, VerifiedBook.pairing_reason, Citation.confidence,
                           VerifiedBook.review_choice, VerifiedBook.purchase_title,
                           VerifiedBook.review_applied_at, VerifiedBook.reviewed_at)
                    .join(Citation, Citation.id == VerifiedBook.citation_id)
                    .join(Course, Course.course_id == Citation.course_id)
                    .where(*conditions, _not_superseded()),
                    join_course=False,
                )
            ).all()
            return [
                PendingItem(vb_id, cit_id, course_id, course_name, enrolled or 0, title, _authors(authors),
                            matched_title, _authors(matched_authors), source, bool(verified), verdict, judge,
                            reason, confidence or 0.0, choice, purchase_title, applied_at is not None, reviewed_at)
                for (vb_id, cit_id, course_id, course_name, enrolled, title, authors, matched_title, matched_authors,
                     source, verified, verdict, judge, reason, confidence, choice, purchase_title, applied_at,
                     reviewed_at) in rows
            ]

        pending_items = review_items(VerifiedBook.review_status == "pending")
        decided_items = review_items(VerifiedBook.review_choice.is_not(None))     # 「已決定」分頁（可撤銷）
        pending_apply = len(pending_apply_ids(s))      # 全校的待套用數：套用永遠處理全部
        cart_items = _load_cart(s)                      # 全校的購物車（頁首購物車圖示的數字），不隨學系篩選

    from library_agent.config import get_settings

    threshold = get_settings().human_review_confidence_threshold
    review = group_pending(pending_items, rec_of, threshold)
    by_kind = group_pending(decided_items, rec_of, threshold)
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    decided = sorted((g for kind in KINDS for g in by_kind[kind]), key=lambda g: g.reviewed_at or earliest, reverse=True)
    kpis["copies_final"] = sum(r.final for r in recs)      # 購物車冊數：各書的決定冊數總和（kpis["copies"] 是系統建議）
    kpis["cart_books"] = sum(r.in_cart for r in recs)      # 購物車書數
    return {"kpis": kpis, "priority": prio, "status": status, "recs": recs, "pending": pending,
            "review": review, "decided": decided, "pending_apply": pending_apply,
            "cart": {"books": len(cart_items), "copies": sum(i["copies"] for i in cart_items)}}


# ---------- 呈現 ----------

def _bars(items: list[tuple[str, int, str]]) -> str:
    maxv = max((v for _, v, _ in items), default=0) or 1
    rows = []
    for label, value, color in items:
        pct = round(value / maxv * 100, 1)
        rows.append(
            f'<div class="bar-row" title="{html.escape(label)}: {value}">'
            f'<span class="bar-label">{html.escape(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct}%;background:{color}"></span></span>'
            f'<span class="bar-value">{value}</span></div>'
        )
    return "\n".join(rows)


def _kpi_tiles(k: dict) -> str:
    # (標籤, 值, 樣式, 元素 id)：id 讓前端在改冊數、加入、移出購物車時就地更新
    tiles = [
        ("課程數", k["courses"], "", None),
        ("書目數", k["citations"], "", None),
        ("已驗證", k["verified"], "", None),
        ("待人工審核", k["pending"], "warn" if k["pending"] else "", None),
        ("系統建議冊數", k["copies"], "accent", None),                        # 系統建議的總和（只是參考）
        ("購物車冊數", k.get("copies_final", 0), "accent", "kpi-final"),       # 購物車裡各書的決定冊數總和
        ("購物車書數", k.get("cart_books", 0), "", "kpi-cart-books"),
    ]
    out = []
    for lbl, val, cls, el_id in tiles:
        if lbl == "待人工審核":
            out.append(f'<a class="kpi" href="#review"><div class="val {cls}">{val}</div><div class="lbl">{lbl}</div></a>')
        else:
            id_attr = f' id="{el_id}"' if el_id else ""
            out.append(f'<div class="kpi"><div class="val {cls}"{id_attr}>{val}</div><div class="lbl">{lbl}</div></div>')
    return "".join(out)


def _rec_row(priority: str, course: str, title: str, btype: str, status_html: str,
             copies_html: str, rationale: str, btypes: str, course_html: str | None = None,
             rec_id: int | None = None, unverified: bool = False, book_key: str | None = None,
             need: int = 0, cut: bool = False, decided: int | None = None, stale_from: int | None = None,
             final: int = 0, in_cart: bool = False, suggested: int = 0, search: str = "") -> str:
    """btypes：類別 token（required / optional / ai），以空白分隔的清單（一本書涵蓋多個類別時有多個，
    前端用「包含」比對）。rec_id：採購建議的 id，有值才有展開鈕（取 GET /trace/{rec_id}）。
    展開鈕與「未驗證」標記內都不放任何文字（由 CSS 畫），因為匯出 CSV 是讀每格的 textContent。
    unverified：這本書有書目在驗證階段查無此書。
    採購冊數欄（最右）：決定冊數輸入框＋「加入購物車／移出購物車」按鈕。book_key 為 None（舊格式的建議）的列沒有；
    留空＝還沒決定（不在購物車），填 0＝這次不買，填大於 0＝加入購物車。系統建議為 0 時不能直接「加入」（沒有東西可加）。
    in_cart：這本書在購物車裡（決定 > 0）；search：這一列可被搜尋的文字（小寫，這裡負責逸出）。
    「需求 N」（被上限截斷時）與「建議已從 A 變成 B」都由 CSS 用 data-* 畫，輸入框的值也不是文字節點。"""
    pcolor = _PRIORITY_COLOR.get(priority, _PENDING_COLOR)
    plabel = _PRIORITY_LABEL.get(priority, _PENDING_LABEL)
    data_id = f' data-rec="{rec_id}"' if rec_id is not None else ""
    toggle = ('<button class="tbtn" type="button" aria-expanded="false" aria-label="展開決策過程"></button>'
              if data_id else "")
    marker = ('<span class="unv" title="驗證階段查無此書，館藏是用書名直接查詢的結果"></span>'
              if unverified else "")
    unv_attr = ' data-unverified="1"' if unverified else ""
    if book_key is None:
        dec_cell = '<td class="dec"></td>'
    else:
        saved = "" if decided is None else str(decided)
        stale = f' data-stale="{stale_from}" data-now="{html.escape(copies_html)}"' if stale_from is not None else ""
        cart_attr = ' disabled title="系統建議為 0，請直接填冊數"' if (not in_cart and suggested <= 0) else ""
        cart_label = "移出購物車" if in_cart else "加入購物車"
        dec_cell = (
            f'<td class="dec"{stale}><input class="dec-in{" has" if decided is not None else ""}" type="number" '
            f'min="0" max="9999" step="1" data-key="{html.escape(book_key)}" data-saved="{saved}" '
            f'placeholder="尚未決定" value="{saved}" aria-label="決定冊數">'
            f'<button class="abtn cartbtn" type="button" data-cart{cart_attr}>{cart_label}</button>'
            f'<div class="aerr" hidden></div></td>'
        )
    incart_attr = ' data-incart="1"' if in_cart else ""
    return (
        f'<tr data-priority="{html.escape(priority)}" data-btypes="{html.escape(btypes)}" '
        f'data-final="{final}" data-search="{html.escape(search)}"{incart_attr}{data_id}{unv_attr}>'
        f'<td class="chk"><input type="checkbox" class="rowchk"></td>'
        f'<td><span class="pill" style="background:{pcolor}">{html.escape(plabel)}</span></td>'
        f'<td>{course_html if course_html is not None else html.escape(course or "")}</td>'
        f'<td>{toggle}{html.escape(title or "")}{marker}</td>'
        f'<td>{btype}</td>'
        f'<td>{status_html}</td>'
        f'<td class="{"num cut" if cut else "num"}" data-need="{need}">{copies_html}</td>'
        f'<td class="rationale">{html.escape(rationale or "")}</td>'
        f'{dec_cell}</tr>'
    )


# 類別判定（AI 推薦 > 指定 > 參考）搬到 book_kind，追溯畫面共用；這裡保留原名稱，既有呼叫與測試不用改
_is_ai_recommended = is_ai_recommended
_book_type_key = book_type_key

_KEY_LABEL = {"required": "指定", "optional": "參考", "ai": "AI推薦"}


def _tag_for_key(key: str) -> str:
    """類別標籤。AI 推薦以橘色標籤與教師指定/參考區隔。"""
    if key == "ai":
        return '<span class="tag tag-ai">AI推薦</span>'
    return f'<span class="tag">{_KEY_LABEL[key]}</span>'


def _course_cell(courses: list) -> str:
    """「課程」欄：列出這本書用到的所有課，每門課旁標明它自己的類別；屬於所選學系的課加粗。"""
    parts = []
    for c in courses:
        text = f"{html.escape(c.name)}（{_KEY_LABEL[c.key]}）" + (f"×{c.sections}" if c.sections > 1 else "")
        parts.append(f'<span class="c-in">{text}</span>' if c.in_scope else text)
    return "、".join(parts)


# 類別篩選按鈕：(token, 顯示文字)。token 與 _book_type_key 的回傳值、列上的 data-btypes 一致。
_CATEGORY_META = [("required", "指定"), ("optional", "參考"), ("ai", "AI 推薦")]


def _category_btns() -> str:
    return '<button class="cbtn active" data-c="all">全部</button>' + "".join(
        f'<button class="cbtn" data-c="{k}">{html.escape(n)}</button>' for k, n in _CATEGORY_META
    )


def _rec_table(recs: list) -> str:
    if not recs:
        return '<p class="empty">尚無採購建議，請先執行 pipeline。</p>'
    body = []
    for r in recs:
        scolor = _STATUS_COLOR.get(r.status, "#898781")
        slabel = _STATUS_LABEL.get(r.status, r.status)
        keys = sorted({c.key for c in r.courses}, key=_KEY_RANK.get)   # 這本書涵蓋的所有類別
        status_html = f'<span class="pill" style="background:{scolor}">{html.escape(slabel)}</span>'
        body.append(_rec_row(
            r.priority, "", r.title, "".join(_tag_for_key(k) for k in keys), status_html,
            str(r.copies), r.rationale, " ".join(keys), course_html=_course_cell(r.courses),
            rec_id=r.rec_id or None, unverified=r.unverified, book_key=r.book_key, need=r.need,
            cut=r.need > r.copies, decided=r.decided, stale_from=r.stale_from, final=r.final,
            in_cart=r.in_cart, suggested=r.copies,
            search=" ".join([*(c.name for c in r.courses), r.title or ""]).lower(),
        ))
    return (
        '<table id="rectable"><thead><tr>'
        '<th class="chk"><input type="checkbox" id="rowchkall" title="全選/全不選"></th>'
        '<th>優先級</th><th>課程</th><th>書名</th><th>類別</th>'
        '<th>館藏狀態</th><th>系統建議</th><th>理由</th><th>採購冊數</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


# 審核分頁：(kind, 分頁名稱, 給新手的一句說明)
_REVIEW_TABS = [
    ("mismatch", "配錯", "稽核判定「配到的書」與課綱列的不是同一本。這些書<b>不在</b>採購建議裡，要人工確認後才會納入。"),
    ("notfound", "查無此書", "驗證階段在權威書目查不到這本書，館藏是用書名直接查詢的結果。這些書<b>仍在</b>採購建議裡，請留意。"),
    ("other", "其他", "其他需要人工確認的書。"),
]
_JUDGE_LABEL = {"rule": "字串規則", "llm": "LLM"}


def _names(authors: list) -> str:
    return html.escape("、".join(authors)) if authors else '<span class="rdim">（無作者）</span>'


def _review_reason(g) -> str:
    if g.kind == "mismatch":
        by = _JUDGE_LABEL.get(g.judge, "稽核")
        return f"配錯（{html.escape(by)}）" + (f"：{html.escape(g.reason)}" if g.reason else "")
    if g.kind == "notfound":
        return "驗證階段查無此書" + ("；解析信心偏低" if g.low_confidence else "")
    return "解析信心偏低，驗證階段送審" if g.low_confidence else "等待人工審核"


def _review_list_status(g) -> str:
    """這組在採購建議裡的狀況：有建議就顯示優先級與冊數；配錯的被擋下。"""
    if g.priority is not None:
        color = _PRIORITY_COLOR.get(g.priority, _PENDING_COLOR)
        label = _PRIORITY_LABEL.get(g.priority, g.priority)
        return f'<span class="pill" style="background:{color}">{html.escape(label)}</span> {g.copies} 冊'
    return "已擋下，不在採購建議" if g.kind == "mismatch" else "不在採購建議"


_CHOICE_LABEL = {"matched": "買配對到的書", "original": "買原書", "custom": "改買其他書名", "none": "都不買"}


def _abtn(act: str, label: str, *, negative: bool = False) -> str:
    cls = "abtn abtn-no" if negative else "abtn"
    return f'<button class="{cls}" type="button" data-act="{act}">{label}</button>'


def _action_cell(g) -> str:
    """這一組的處理動作，三個待處理分頁（配錯／查無此書／其他）完全一樣：書名輸入框（預填原書名，多數情況只要確認或改個錯字）
    ＋ISBN（選填）＋［加入採購建議］［都不買］。有配到書的列多一顆「改填配到的書名」：只把配到的書名填進輸入框，
    不送出，使用者看過再按加入；所以所有決定都走同一條路（輸入框的內容就是要買的書）。
    輸入的書名等於原書名且沒填 ISBN 時，後端視同「買原書」（review_actions）。"""
    fill = (f'<button class="abtn" type="button" data-fill="{html.escape(g.matched_title, quote=True)}">改填配到的書名</button>'
            if g.kind != "notfound" and g.matched_title else "")
    body = (
        f'<input class="atitle" type="text" maxlength="500" value="{html.escape(g.title or "")}" aria-label="要採購的書名">'
        '<input class="aisbn" type="text" maxlength="20" placeholder="ISBN（選填）" aria-label="ISBN（選填）">'
        + _abtn("custom", "加入採購建議") + fill + _abtn("none", "都不買", negative=True)
    )
    return f'<td class="ract">{body}<div class="aerr" hidden></div></td>'


def _courses_cell(g) -> str:
    courses = "、".join(f"{html.escape(n)} {e}" for n, e in g.courses)
    items_note = f' <span class="rdim">（{g.items} 個書目）</span>' if g.items > 1 else ""
    return f"{courses}{items_note}"


def _review_row(g) -> str:
    """審核列：勾選｜優先級｜課程｜▸書名｜配到的書｜原因｜人數合計｜處理（與採購表同一套骨架）。"""
    if g.kind == "notfound":
        matched = f'<span class="rdim">查無記錄（來源：{html.escape(g.source or "—")}）</span>'
        searchable = [g.title or ""]
    else:
        matched = (f'<span class="bm">{html.escape(g.matched_title or "")}</span><div class="rdim">{_names(g.matched_authors)}'
                   f' · {html.escape(g.source or "—")}</div>')
        searchable = [g.title or "", g.matched_title or ""]
    search = " ".join([*searchable, *(n for n, _ in g.courses)]).lower()
    ids = ",".join(str(i) for i in g.vb_ids)
    return (
        f'<tr data-ids="{ids}" data-priority="{html.escape(g.priority or "none")}" data-search="{html.escape(search)}">'
        '<td class="chk"><input type="checkbox" class="rchk"></td>'
        f'<td>{_review_list_status(g)}</td>'
        f'<td>{_courses_cell(g)}</td>'
        '<td><button class="tbtn" type="button" aria-expanded="false" aria-label="展開決策過程"></button>'
        f'<span class="bt">{html.escape(g.title or "")}</span><div class="rdim">{_names(g.authors)}</div></td>'
        f'<td>{matched}</td><td>{_review_reason(g)}</td>'
        f'<td class="num">{g.enrolled_total}</td>'
        f'{_action_cell(g)}</tr>'
    )


def _decided_row(g) -> str:
    """審核紀錄列：勾選｜課程｜▸書名｜決定｜採購書名｜狀態｜處理（撤銷）。"""
    label = _CHOICE_LABEL.get(g.choice, g.choice or "")
    if g.choice == "none":
        bought, state = "—", "已退回"
    else:
        bought = html.escape(g.buy_title or g.matched_title or "")
        state = "已套用" if g.applied else "待套用"
    ids = ",".join(str(i) for i in g.vb_ids)
    search = " ".join([g.title or "", g.buy_title or "", *(n for n, _ in g.courses)]).lower()
    return (
        f'<tr data-ids="{ids}" data-priority="none" data-search="{html.escape(search)}">'
        '<td class="chk"><input type="checkbox" class="rchk"></td>'
        f'<td>{_courses_cell(g)}</td>'
        '<td><button class="tbtn" type="button" aria-expanded="false" aria-label="展開決策過程"></button>'
        f'<span class="bt">{html.escape(g.title or "")}</span><div class="rdim">{_names(g.authors)}</div></td>'
        f'<td>{html.escape(label)}</td><td>{bought}</td><td>{state}</td>'
        '<td><button class="abtn" type="button" data-undo>撤銷</button><div class="aerr" hidden></div></td></tr>'
    )


def _purchase_batch_bars() -> str:
    """採購建議的批次列。每個按鈕都是 POST /purchase/decide-many 的一個 mode；沒有「全部改成 N 本」，
    一次把上百本改成同一冊數太容易出錯。購物車在頁首的抽屜裡（那裡有自己的移出／清空）。"""
    return (
        '<div class="rbatch">勾選的書：'
        '<button class="abtn" type="button" data-pbatch="suggested">加入購物車（採用系統建議）</button>'
        '<button class="abtn abtn-no" type="button" data-pbatch="zero">設為不買</button>'
        '<button class="abtn" type="button" data-pbatch="clear">清除決定</button></div>'
    )


def _log_batch_bar() -> str:
    return ('<div class="rbatch">勾選的組：'
            '<button class="abtn" type="button" data-ubatch="undo">撤銷所選</button></div>')


def _review_panel(review: dict, decided: list | None = None, pending_apply: int = 0,
                  rec_html: str | None = None, rec_count: int = 0, toolbar_html: str | None = None) -> str:
    """採購建議與人工審核的同一張卡片，兩層導覽：第一層「採購｜審核」（兩件事性質不同：審核是修資料、採購是做決定），
    採購區只有採購建議一張表；審核區有子分頁 配錯｜查無此書｜其他｜審核紀錄 與「套用」橫幅。
    審核有待處理的書或決定紀錄才有第一層（沒有審核可看時只有採購建議，不放第一層）。
    購物車不是分頁，是頁首左上角的購物車抽屜（見 _render）。每個面板有 data-filters（工具列要顯示哪些篩選）
    與 data-export（匯出哪種欄位）。
    沒有 rec_html 時只輸出審核部分（單獨成卡片）。"""
    decided = decided or []
    total = sum(len(review.get(k, [])) for k, _, _ in _REVIEW_TABS)
    has_review = total > 0 or bool(decided)

    def banner_for(top_attr: str) -> str:
        return (
            f'<div class="rapply"{top_attr}><span>{pending_apply} 筆決定待套用</span>'
            '<button id="applybtn" class="runbtn" type="button" style="font-size:13px;padding:6px 14px;">套用並重算採購建議</button>'
            '<span class="rdim">套用後，放行的書才會出現在採購建議</span></div>'
            if pending_apply > 0 else ""
        )

    standalone_head = '<div class="cardhead"><h2>人工審核</h2></div>'
    if rec_html is None and not has_review:
        return f'<div class="card" id="review">{standalone_head}{banner_for("")}<p class="empty">目前沒有待審書目。</p></div>'

    # 待審分頁預設開在組數最多的（並列取排在前面的）；待審都處理完了才開「審核紀錄」
    review_open = ""
    if has_review:
        review_open = "decided" if total == 0 else max((k for k, _, _ in _REVIEW_TABS), key=lambda k: len(review.get(k, [])))
    opening = "recs" if rec_html is not None else review_open
    has_top = rec_html is not None and has_review          # 兩層導覽：採購與審核都有東西才需要第一層
    top_open = "buy" if opening == "recs" else "review"    # 預設開在哪一層（有採購建議時一定是採購）

    def top_attrs(group: str) -> str:
        """屬於某一層的元素：標上 data-top，不在預設那一層的先隱藏（沒有 JS 也是對的）。沒有第一層時什麼都不標。"""
        return f' data-top="{group}"' + ("" if group == top_open else " hidden") if has_top else ""

    def tab(kind: str, label: str) -> str:
        return (f'<button class="rtab{" active" if kind == opening else ""}" data-k="{kind}"'
                f'{top_attrs("buy" if kind == "recs" else "review")}>{label}</button>')

    def panel(kind: str, inner: str, filters: str, export: str, extra: str = "") -> str:
        hidden = "" if kind == opening else " hidden"
        return (f'<div class="rpanel" data-k="{kind}" data-filters="{filters}" data-export="{export}"{extra}{hidden}>'
                f'{inner}</div>')

    tabs, panels = [], []
    if rec_html is not None:
        tabs.append(tab("recs", f"採購建議 {rec_count} 本"))
        panels.append(panel("recs", rec_html, "prio cat", "recs"))
    if has_review:
        for kind, label, note in _REVIEW_TABS:
            groups = review.get(kind, [])
            tabs.append(tab(kind, f"{label} {len(groups)} 組（{sum(g.items for g in groups)} 筆書目）"))
            if groups:
                body = (
                    '<table class="rtbl"><thead><tr><th class="chk"><input type="checkbox" class="rchkall" title="全選/全不選"></th>'
                    '<th>優先級</th><th>用到的課（人數）</th><th>原書（課綱列的）</th><th>配到的書</th>'
                    '<th>原因</th><th>人數合計</th><th>處理</th></tr></thead>'
                    f'<tbody>{"".join(_review_row(g) for g in groups)}</tbody></table>'
                )
            else:
                body = '<p class="empty">這個分類目前沒有待審的書。</p>'
            panels.append(panel(kind, f'<p class="rnote">{note}</p>{body}', "" if kind == "mismatch" else "prio", "review"))
        if decided:
            tabs.append(tab("decided", f"審核紀錄 {len(decided)} 組"))
            panels.append(panel("decided", (
                '<p class="rnote">做過的審核決定。按「撤銷」會改回待審；'
                '放行的書要按上方「套用並重算採購建議」才會反映到採購建議。</p>'
                + _log_batch_bar()
                + '<table class="rtbl"><thead><tr><th class="chk"><input type="checkbox" class="rchkall" title="全選/全不選"></th>'
                '<th>用到的課</th><th>原書（課綱列的）</th><th>決定</th><th>採購書名</th><th>狀態</th><th>處理</th></tr></thead>'
                f'<tbody>{"".join(_decided_row(g) for g in decided)}</tbody></table>'), "", "log"))
    # 有採購建議時一定有分頁列（至少「採購建議」）；單獨的審核卡片只在有審核項目時才有。
    # 有第一層時，採購區只有一個分頁，子分頁列只在審核區顯示。
    bar_hidden = " hidden" if has_top and top_open == "buy" else ""
    bar = (f'<div class="filters maintabs"{bar_hidden}>{"".join(tabs)}</div>'
           if (rec_html is not None or has_review) else "")
    toplevel = ""
    hint = ""
    if has_top:
        def toptab(group: str, label: str) -> str:
            return f'<button class="toptab{" active" if group == top_open else ""}" data-top="{group}" type="button">{label}</button>'

        toplevel = ('<div class="toplevel">' + toptab("buy", f"採購 {rec_count} 本")
                    + toptab("review", f"審核 {total} 組待處理") + '</div>')
        if pending_apply > 0:      # 採購區：提醒有決定還沒套用（套用本身只在審核區）
            hint = (f'<div class="applyhint" data-top="buy"{"" if top_open == "buy" else " hidden"}>'
                    f'<span>有 {pending_apply} 筆審核決定尚未套用，放行的書還沒出現在這裡</span>'
                    '<button class="abtn" type="button" data-goto-top="review">前往審核</button></div>')
    head = standalone_head if rec_html is None else ""
    return (f'<div class="card" id="review" data-review-open="{review_open}">{head}{toplevel}'
            f'{banner_for(top_attrs("review"))}{hint}{bar}{toolbar_html or ""}{"".join(panels)}</div>')


def _render(data: dict) -> str:
    # 延後 import：graph 會把所有 agent 模組拉進來，與本檔其他用到 graph 的地方一致
    from library_agent.graph import _MAX_DISCOVERY_ITERATIONS

    prio_bars = _bars(
        [(_PRIORITY_LABEL[k], data["priority"].get(k, 0), _PRIORITY_COLOR[k])
         for k, _, _ in _PRIORITY_META]
        + [(_PENDING_LABEL, data["kpis"]["pending"], _PENDING_COLOR)]
    )
    status_bars = _bars([(_STATUS_LABEL[k], data["status"].get(k, 0), _STATUS_COLOR[k])
                        for k, _, _ in _STATUS_META])
    filter_btns = '<button class="fbtn active" data-f="all">全部</button>' + "".join(
        f'<button class="fbtn" data-f="{k}">{html.escape(n)}</button>' for k, n, _ in _PRIORITY_META
    )
    # 採購建議與人工審核是同一張卡片的分頁；採購建議是第一個、預設開啟的分頁。
    # 工具列（搜尋、篩選、匯出）只有一條，對目前開著的分頁生效；篩選與匯出的欄位由面板的 data-filters／data-export 決定。
    toolbar = (
        '<div class="toolbar" id="toolbar">'
        '<input id="recsearch" class="searchbox" type="search" placeholder="搜尋課程或書名…">'
        f'<div class="tb-prio">{filter_btns}</div>'
        f'<div class="tb-cat">{_category_btns()}</div>'
        '<div class="ffwrap tb-export">'
        '<button id="expbtn" class="uploadbtn" type="button">匯出 CSV ▾</button>'
        '<div id="exppanel" class="filefilter" hidden>'
        '<div class="exp-prio"><div class="exp-sec">依優先級匯出：</div>'
        '<label><input type="checkbox" class="expchk" value="high" checked> HIGH 高</label>'
        '<label><input type="checkbox" class="expchk" value="medium" checked> MEDIUM 中</label>'
        '<label><input type="checkbox" class="expchk" value="low" checked> LOW 低</label>'
        '<label><input type="checkbox" class="expchk" value="skip" checked> SKIP 略過</label></div>'
        '<div class="exp-sec"><label><input type="checkbox" id="exponlychecked"> 只匯出表格中我勾選的列</label></div>'
        '<button id="expgo" class="runbtn" type="button" style="margin-top:8px;font-size:13px;padding:6px 14px;">下載 CSV</button>'
        '</div></div></div>'
    )
    recs = data["recs"]
    main_card = _review_panel(
        data["review"], data.get("decided"), data.get("pending_apply", 0),
        rec_html=_purchase_batch_bars() + _rec_table(recs), rec_count=len(recs), toolbar_html=toolbar,
    )
    cart = data.get("cart") or {}      # 全校的購物車合計（不隨學系篩選），頁首購物車圖示上的數字
    # 抽屜的 CSV 要把代碼翻成畫面上同樣的字，標籤只在 Python 這裡有一份
    _plabel_json = json.dumps(_PRIORITY_LABEL, ensure_ascii=False)
    _slabel_json = json.dumps(_STATUS_LABEL, ensure_ascii=False)
    cart_header = (
        '<div class="topbar">'
        '<button id="cartbtn" class="carttop" type="button" aria-label="購物車" title="購物車" '
        'aria-expanded="false" aria-controls="cartdrawer">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>'
        '<path d="M2 3h3l2.7 12.4a1 1 0 0 0 1 .8h8.9a1 1 0 0 0 1-.8L20.5 7H6"/></svg>'
        f'<span id="cartcount" class="cartcount{" zero" if not cart.get("books") else ""}">{int(cart.get("books") or 0)}</span>'
        '</button><h1>圖書館採購決策儀表板</h1></div>'
    )
    cart_drawer = (
        '<div id="cartshade" hidden></div>'
        '<aside id="cartdrawer" class="cartdrawer" role="dialog" aria-label="購物車" hidden>'
        '<div class="cdhead"><h2>購物車（全校）</h2>'
        '<button id="cartclose" class="cdclose" type="button" aria-label="關閉">×</button></div>'
        '<div id="cartlist" class="cdlist"></div>'
        '<div class="cdfoot"><div id="carttotal" class="cdtotal"></div>'
        '<button id="cartexport" class="runbtn" type="button" style="font-size:13px;padding:6px 14px;">匯出購物車 CSV</button>'
        '<button id="cartclear" class="abtn abtn-no" type="button">清空購物車</button></div>'
        '</aside>'
    )
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>採購決策儀表板</title>
<style>
:root {{
  color-scheme: light;                /* 強制淺色，不隨 OS 深色模式變黑 */
  --page:#f4f5f7; --surface:#ffffff; --text-primary:#1f2328; --text-secondary:#57606a;
  --muted:#8a8f98; --gridline:#eaecef; --track:#eef1f4; --border:#e2e5ea;
  --critical:#d03b3b; --warning:#e0900a;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--page); color:var(--text-primary);
  font-family:system-ui,-apple-system,"Segoe UI","Microsoft JhengHei",sans-serif; line-height:1.5; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:24px 20px 60px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:var(--text-secondary); font-size:13px; margin-bottom:20px; }}
h2 {{ font-size:15px; margin:0 0 12px; color:var(--text-secondary); }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:16px; }}
/* 終端文字 log：深色底、等寬字，空的時候顯示提示 */
.runlog {{ margin:0; padding:12px 14px; border-radius:8px; background:#1f2328; color:#e6edf3;
  font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,"Microsoft JhengHei",monospace;
  max-height:340px; min-height:120px; overflow:auto; white-space:pre-wrap; word-break:break-all; }}
.runlog:empty::before {{ content:"（尚無執行紀錄；按「執行」後這裡會即時顯示終端機的輸出）"; color:#8b949e; }}
.flownote {{ margin:8px 0 0; text-align:center; font-size:12px; line-height:1.6;
  color:var(--text-secondary); }}
.flownote b {{ color:var(--text-primary); font-weight:600; }}
.runbar {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
.runbtn {{ font:inherit; font-size:14px; font-weight:600; padding:8px 18px; border-radius:8px; cursor:pointer;
  border:1px solid var(--border); background:var(--text-primary); color:var(--surface); }}
.runbtn:disabled {{ opacity:.45; cursor:not-allowed; }}
.runstatus {{ font-size:13px; font-weight:600; color:var(--text-secondary); }}
.runstatus.running {{ color:var(--warning); }}
.runstatus.done {{ color:#0ca30c; }}
.runstatus.error {{ color:var(--critical); }}
.counts {{ font-size:12.5px; color:var(--muted); font-variant-numeric:tabular-nums; }}
.uploadbtn {{ font-size:13px; padding:6px 12px; border-radius:8px; cursor:pointer;
  border:1px solid var(--border); background:transparent; color:var(--text-secondary); }}
.uploadbtn:hover {{ background:var(--track); }}
.ffwrap {{ position:relative; }}
.filefilter {{ position:absolute; top:calc(100% + 4px); left:0; z-index:20; min-width:220px;
  max-height:280px; overflow-y:auto; display:flex; flex-direction:column; gap:6px;
  padding:10px 12px; font-size:12.5px; color:var(--text-secondary);
  background:var(--surface); border:1px solid var(--border); border-radius:8px;
  box-shadow:0 4px 14px rgba(0,0,0,.1); }}
.filefilter[hidden] {{ display:none; }}
.filefilter label {{ display:flex; align-items:center; gap:6px; cursor:pointer; white-space:nowrap; }}
.filefilter .ff-actions {{ display:flex; gap:10px; padding-bottom:6px; margin-bottom:2px;
  border-bottom:1px solid var(--gridline); }}
.filefilter .ff-actions a {{ color:var(--text-secondary); cursor:pointer; text-decoration:underline; }}
.filefilter .ff-empty {{ color:var(--muted); }}
.dept-college {{ margin-top:4px; padding-top:4px; border-top:1px solid var(--gridline); }}
.dept-col-lbl {{ font-size:12.5px; }}
.dept-item {{ padding-left:18px; color:var(--text-secondary); }}
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:16px; }}
.kpi {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }}
.kpi .val {{ font-size:30px; font-weight:700; letter-spacing:-.5px; }}
.kpi .val.accent {{ color:var(--critical); }}
.kpi .val.warn {{ color:var(--warning); }}
.kpi .lbl {{ font-size:12px; color:var(--text-secondary); margin-top:2px; }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:720px) {{ .charts {{ grid-template-columns:1fr; }} }}
.tables {{ display:block; }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:8px 0; }}
.bar-label {{ width:96px; flex:none; text-align:right; font-size:13px; color:var(--text-secondary); }}
.bar-track {{ flex:1; height:20px; background:var(--track); border-radius:4px; }}
.bar-fill {{ height:100%; border-radius:0 4px 4px 0; min-width:2px; display:block; }}
.bar-value {{ width:44px; flex:none; font-weight:600; font-variant-numeric:tabular-nums; }}
.bar-row:hover .bar-fill {{ filter:brightness(1.08); }}
.filters {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }}
.fsep {{ width:1px; height:18px; background:var(--border); margin:0 6px; }}
.fbtn, .cbtn, .rtab {{ font:inherit; font-size:12.5px; padding:4px 12px; border-radius:99px; cursor:pointer;
  border:1px solid var(--border); background:transparent; color:var(--text-secondary); }}
.fbtn.active, .cbtn.active, .rtab.active {{ background:var(--text-primary); color:var(--surface); border-color:var(--text-primary); }}
a.kpi {{ display:block; text-decoration:none; color:inherit; }}
.unv {{ display:inline-block; margin-left:6px; padding:0 6px; border-radius:5px; border:1px solid #e0900a;
  color:#a05f00; font-size:11px; vertical-align:middle; cursor:help; }}
.unv::after {{ content:"未驗證"; }}
.rnote {{ margin:0 0 10px; font-size:12.5px; color:var(--text-secondary); }}
.rtbl td {{ font-size:12.5px; }}
.rdim {{ color:var(--text-secondary); }}
.toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:0 0 12px; }}
.tb-prio, .tb-cat {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
.tb-prio[hidden], .tb-cat[hidden], .exp-prio[hidden] {{ display:none; }}
.tb-export {{ margin-left:auto; }}
.cartbtn {{ display:block; width:100%; margin:4px 0 0; white-space:nowrap; text-align:center; }}
.topbar {{ display:flex; align-items:center; gap:14px; margin:0 0 4px; }}
.topbar h1 {{ margin:0; }}
.carttop {{ position:relative; display:inline-flex; align-items:center; justify-content:center; width:42px; height:42px;
  padding:0; border:1px solid var(--border); border-radius:10px; background:var(--surface); color:var(--text-primary); cursor:pointer; }}
.carttop:hover {{ border-color:var(--text-secondary); }}
.carttop svg {{ width:22px; height:22px; }}
.cartcount {{ position:absolute; top:-7px; right:-7px; min-width:20px; height:20px; padding:0 5px; border-radius:10px;
  background:#1f6feb; color:#fff; font-size:12px; font-weight:700; line-height:20px; text-align:center; }}
.cartcount.zero {{ background:var(--muted); }}
#cartshade {{ position:fixed; inset:0; background:rgba(0,0,0,.28); z-index:40; }}
.cartdrawer {{ position:fixed; top:0; left:0; bottom:0; width:min(460px,100vw); background:var(--surface);
  border-right:1px solid var(--border); box-shadow:2px 0 16px rgba(0,0,0,.18); z-index:50; display:flex; flex-direction:column; }}
.cartdrawer[hidden], #cartshade[hidden] {{ display:none; }}
.cdhead {{ display:flex; align-items:center; justify-content:space-between; padding:14px 16px; border-bottom:1px solid var(--gridline); }}
.cdhead h2 {{ margin:0; font-size:17px; }}
.cdclose {{ font:inherit; font-size:24px; line-height:1; border:none; background:none; cursor:pointer; color:var(--text-secondary); }}
.cdlist {{ flex:1; overflow-y:auto; padding:4px 16px; }}
.ci {{ display:grid; grid-template-columns:1fr auto auto; gap:4px 10px; align-items:center; padding:10px 0; border-bottom:1px solid var(--gridline); }}
.ci-title {{ font-size:13.5px; font-weight:600; overflow-wrap:anywhere; }}
.ci-sub {{ grid-column:1; font-size:11.5px; color:var(--text-secondary); }}
.ci-in {{ width:72px; font:inherit; font-size:13px; font-weight:700; padding:4px 6px; border:1px solid #1f6feb; border-radius:6px; }}
.ci .abtn {{ margin:0; }}
.ci .aerr {{ grid-column:1 / -1; }}
.cdfoot {{ padding:12px 16px; border-top:1px solid var(--gridline); display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.cdtotal {{ flex:1 1 100%; font-size:14px; font-weight:700; }}
td.num.cut::after {{ content:"（需求 " attr(data-need) "）"; font-weight:400; font-size:11px; color:#a05f00; white-space:nowrap; }}
td.dec {{ width:132px; min-width:132px; }}
.dec-in {{ display:block; width:100%; box-sizing:border-box; font:inherit; font-size:12.5px; padding:3px 6px; border:1px solid var(--border); border-radius:6px; }}
.dec-in.has {{ border-color:#1f6feb; font-weight:700; }}
td.dec[data-stale]::after {{ content:"建議已從 " attr(data-stale) " 變成 " attr(data-now); display:block; font-size:11px; color:#a05f00; }}
.maintabs {{ margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--gridline); }}
.tabspacer {{ flex:1; }}
.toplevel {{ display:flex; gap:4px; margin:0 0 14px; border-bottom:2px solid var(--gridline); }}
.toptab {{ font:inherit; font-size:15px; font-weight:600; padding:9px 20px; margin-bottom:-2px; border:none;
  border-bottom:3px solid transparent; background:none; color:var(--text-secondary); cursor:pointer; }}
.toptab:hover {{ color:var(--text-primary); }}
.toptab.active {{ color:var(--text-primary); border-bottom-color:#1f6feb; }}
.applyhint {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin:0 0 12px; padding:8px 12px;
  border-radius:8px; border:1px solid #e0900a; background:rgba(224,144,10,.10); font-size:13px; }}
.applyhint .abtn {{ margin:0; }}
.maintabs[hidden], .rtab[hidden], .rapply[hidden], .applyhint[hidden] {{ display:none; }}
.ract {{ min-width:230px; }}
.abtn {{ font:inherit; font-size:12px; padding:3px 10px; margin:0 4px 4px 0; border-radius:6px; cursor:pointer;
  border:1px solid var(--border); background:var(--surface); color:var(--text-primary); }}
.abtn:hover {{ border-color:var(--text-secondary); }}
.abtn:disabled {{ opacity:.5; cursor:not-allowed; }}
.abtn-no {{ color:#a33; }}
.atitle {{ display:block; width:100%; font:inherit; font-size:12.5px; padding:3px 6px; margin-bottom:4px;
  border:1px solid var(--border); border-radius:6px; }}
.aisbn {{ display:block; width:100%; font:inherit; font-size:12.5px; padding:3px 6px; margin-bottom:4px;
  border:1px solid var(--border); border-radius:6px; }}
.aerr {{ color:var(--critical); font-size:12px; margin-top:2px; }}
.rbatch {{ margin:0 0 8px; font-size:12.5px; color:var(--text-secondary); }}
.rapply {{ margin:0 0 10px; padding:8px 12px; border-radius:8px; border:1px solid #e0900a;
  background:rgba(224,144,10,.10); font-size:13px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
tr.rdone td {{ opacity:.45; transition:opacity .4s; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--gridline); padding:8px 10px; text-align:left; vertical-align:top; }}
th {{ color:var(--text-secondary); font-weight:600; white-space:nowrap; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }}
td.rationale {{ color:var(--text-secondary); font-size:12px; min-width:190px; max-width:340px; }}
#rectable td:nth-child(3) {{ min-width:170px; }}
.pill {{ display:inline-block; padding:1px 9px; border-radius:99px; font-size:11.5px; font-weight:600; color:#fff; white-space:nowrap; }}
.tag {{ display:inline-block; padding:1px 7px; border-radius:5px; font-size:11.5px; border:1px solid var(--border); color:var(--text-secondary); white-space:nowrap; }}
.tag-ai {{ border-color:#e0900a; color:#fff; background:#e0900a; }}
.c-in {{ font-weight:700; color:var(--text-primary); }}
.tbtn {{ width:18px; height:18px; margin-right:6px; padding:0; border:1px solid var(--border);
  border-radius:4px; background:var(--surface); color:var(--text-secondary); cursor:pointer; vertical-align:middle; }}
.tbtn::before {{ content:"▸"; font-size:11px; line-height:1; }}
.tbtn[aria-expanded="true"]::before {{ content:"▾"; }}
tr.trace-row > td {{ background:var(--gridline); padding:14px 18px; }}
.trace {{ font-size:13px; line-height:1.55; max-width:1100px; }}
.tr-title {{ margin:0 0 4px; font-size:15px; }}
.tr-rationale {{ color:var(--text-secondary); margin-bottom:8px; }}
.tr-sec {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:8px 14px 10px; margin-top:10px; }}
.tr-sec h4 {{ margin:0 0 6px; font-size:13px; }}
.tr-dim {{ color:var(--text-secondary); }}
.tr-mention {{ margin:4px 0; padding:4px 10px; border-left:3px solid var(--border); color:var(--text-secondary); }}
.tr-pre {{ white-space:pre-wrap; max-height:240px; overflow:auto; font:inherit; font-size:12px; }}
.tr-tbl th, .tr-tbl td {{ padding:4px 8px; font-size:12px; }}
.tr-pair {{ display:flex; gap:12px; flex-wrap:wrap; align-items:flex-start; padding:8px; border:1px solid var(--border); border-radius:6px; margin-bottom:6px; }}
.tr-side {{ flex:1 1 280px; }}
.tr-side h5 {{ margin:0 0 4px; font-size:12px; color:var(--text-secondary); }}
.tr-mid {{ align-self:center; color:var(--text-secondary); }}
.tr-verdict {{ flex-basis:100%; }}
.tr-bad {{ border-color:#c0392b; background:rgba(192,57,43,.08); }}
.tr-tag {{ display:inline-block; padding:0 6px; border-radius:5px; border:1px solid var(--border); font-size:11.5px; }}
.tr-note {{ padding:6px 10px; border-radius:6px; border:1px solid var(--border); background:var(--gridline); }}
.tr-calc {{ margin:4px 0 0; padding-left:20px; }}
.tr-drift {{ margin-top:8px; padding:6px 10px; border-radius:6px; border:1px solid #e0900a; background:rgba(224,144,10,.12); }}
.empty {{ color:var(--muted); font-size:13px; }}
.cardhead {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap; }}
.cardhead h2 {{ margin:0; }}
.headtools {{ display:flex; align-items:center; gap:8px; }}
.searchbox {{ font:inherit; font-size:13px; padding:5px 10px; border:1px solid var(--border);
  border-radius:6px; width:180px; }}
th.chk, td.chk {{ width:28px; text-align:center; padding-left:6px; padding-right:6px; }}
.exp-sec {{ font-weight:600; color:var(--text-secondary); padding-top:4px;
  margin-top:2px; border-top:1px solid var(--gridline); }}
.exp-sec:first-child {{ border-top:none; margin-top:0; padding-top:0; }}
#exppanel {{ right:0; left:auto; }}
</style></head><body><div class="wrap">
{cart_header}
<div class="sub">缺口分析 · 資料來自當下資料庫</div>

<div class="card runbar">
  <button id="runbtn" class="runbtn">▶ 執行 pipeline</button>
  <label class="uploadbtn">上傳 xlsx<input id="fileupload" type="file" accept=".xlsx" multiple hidden></label>
  <div class="ffwrap">
    <button id="ffbtn" class="uploadbtn" type="button">檔案 (全部)</button>
    <div id="filefilter" class="filefilter" hidden></div>
  </div>
  <div class="ffwrap">
    <button id="deptbtn" class="uploadbtn" type="button">學系 (全部)</button>
    <div id="deptfilter" class="filefilter" hidden></div>
  </div>
  <span id="runstatus" class="runstatus">—</span>
  <span id="counts" class="counts"></span>
</div>

<div class="card">
  <h2>Pipeline 執行紀錄</h2>
  <pre id="runlog" class="runlog"></pre>
  <p class="flownote">
    <b>validator</b> 之後若有 AI 推薦書驗證失敗，會回到 discoverer 重推（最多 {_MAX_DISCOVERY_ITERATIONS - 1} 輪）；
    <b>match_auditor</b> 會把配對可疑、或抽取信心度不足的書送進 human_review 佇列，
    並排除在採購建議之外。
  </p>
</div>

<div class="kpi-grid">{_kpi_tiles(data["kpis"])}</div>

<div class="charts">
  <div class="card"><h2>採購優先級分布</h2>{prio_bars}</div>
  <div class="card"><h2>館藏狀態分布</h2>{status_bars}</div>
</div>

<div class="tables">
  {main_card}
</div>
</div>
{cart_drawer}
<script>
// ---- 執行紀錄：把後端複製的終端文字接在文字框後面 ----
let _logEpoch = null, _logCursor = 0, _logLines = [];
let _logLoop = false, _logBusy = false, _logTimer = null;
const _LOG_MAX = 2000;
async function _pollLog() {{
  // 同一時間只讓一個請求在飛：頁面載入時的一次性抓取與「發現正在跑」觸發的輪詢可能重疊，
  // 兩個請求會拿到同一批行，各接一次就重複了。
  if (_logBusy) return;
  _logBusy = true;
  try {{
    const q = '/log?after=' + _logCursor + (_logEpoch !== null ? '&epoch=' + _logEpoch : '');
    const d = await (await fetch(q)).json();
    let changed = false;
    if (d.epoch !== _logEpoch) {{   // 新的一次執行：清空重來，即使它還沒有任何輸出也要清掉
      _logLines = []; _logEpoch = d.epoch; changed = true;
    }}
    _logCursor = d.last;
    if (d.lines.length) {{
      _logLines = _logLines.concat(d.lines).slice(-_LOG_MAX);
      changed = true;
    }}
    if (changed) {{
      const box = _el('runlog');
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;  // 使用者往上捲看舊的就不要硬拉到底
      box.textContent = _logLines.join('\\n');
      if (atBottom) box.scrollTop = box.scrollHeight;
    }}
  }} catch (e) {{ /* 暫時連不上，下一輪再試 */ }}
  _logBusy = false;
  if (_logLoop && !_logTimer) {{
    _logTimer = setTimeout(function() {{ _logTimer = null; _pollLog(); }}, 1000);
  }}
}}

// ---- 分頁、共用工具列與篩選 ----
// 搜尋、優先級／類別篩選、全選、匯出都對「目前開著的分頁」的表生效。
function _activeTab() {{ return document.querySelector('.rtab.active'); }}
function _activeKey() {{ const t = _activeTab(); return t ? t.dataset.k : 'recs'; }}
function _activePanel() {{
  const t = _activeTab();
  return t ? document.querySelector('.rpanel[data-k="' + t.dataset.k + '"]') : null;
}}

// 套用目前分頁的篩選：優先級按鈕與類別按鈕取交集；搜尋框有字時只看搜尋（無視兩組按鈕，維持原行為）。
// 搜尋比對列上的 data-search（伺服器組好的小寫文字，不依欄位位置）。
function _applyRecFilters() {{
  const panel = _activePanel();
  const table = panel ? panel.querySelector('table') : null;
  if (!table) return;
  const kw = (document.getElementById('recsearch').value || '').trim().toLowerCase();
  const pa = document.querySelector('#toolbar .fbtn.active');
  const ca = document.querySelector('#toolbar .cbtn.active');
  const f = pa ? pa.dataset.f : 'all';
  const c = ca ? ca.dataset.c : 'all';
  table.querySelectorAll('tbody tr:not(.trace-row)').forEach(function(tr){{
    let show;
    if (kw) {{
      show = (tr.dataset.search || '').includes(kw);
    }} else {{
      const types = (tr.dataset.btypes || '').split(' ');
      show = (f === 'all' || tr.dataset.priority === f) && (c === 'all' || types.includes(c));
    }}
    tr.style.display = show ? '' : 'none';
    // 展開的追溯列不是資料列：顯示與否完全跟隨上一列（收合與否另由 hidden 屬性管）
    const nx = tr.nextElementSibling;
    if (nx && nx.classList.contains('trace-row')) nx.style.display = show ? '' : 'none';
  }});
}}

// 切換分頁時：重設搜尋、按鈕與勾選，並依面板宣告的 data-filters 顯示或隱藏優先級／類別篩選與匯出的優先級勾選項
function _resetToolbar() {{
  const box = document.getElementById('recsearch');
  if (box) box.value = '';
  document.querySelectorAll('#toolbar .fbtn').forEach(function(x){{ x.classList.toggle('active', x.dataset.f === 'all'); }});
  document.querySelectorAll('#toolbar .cbtn').forEach(function(x){{ x.classList.toggle('active', x.dataset.c === 'all'); }});
  const panel = _activePanel();
  const filters = ((panel && panel.dataset.filters) || '').split(' ');
  [['.tb-prio', 'prio'], ['.tb-cat', 'cat'], ['.exp-prio', 'prio']].forEach(function(pair){{
    const el = document.querySelector(pair[0]);
    if (el) el.hidden = !filters.includes(pair[1]);
  }});
  document.querySelectorAll('.rowchk, .rchk, .rchkall, #rowchkall').forEach(function(c){{ c.checked = false; }});
}}

// 第一層（採購｜審核）：屬於某一層的元素（子分頁按鈕、套用橫幅、待套用提示）只在那一層顯示；
// 採購區只有一個分頁，所以子分頁列只在審核區顯示。任何分頁被選到（含重新整理後還原、KPI 連結）都會同步第一層。
let _lastReview = '';
function _syncTop(top) {{
  document.querySelectorAll('.toptab').forEach(function(b){{ b.classList.toggle('active', b.dataset.top === top); }});
  document.querySelectorAll('#review [data-top]').forEach(function(el){{
    if (!el.classList.contains('toptab')) el.hidden = el.dataset.top !== top;
  }});
  const bar = document.querySelector('.maintabs');
  if (bar && document.querySelector('.toptab')) bar.hidden = (top === 'buy');
}}

// 按第一層：採購＝採購建議；審核＝上次開的審核分頁（沒有就開待審最多的）
function _selectTop(top) {{
  const card = document.getElementById('review');
  if (!card) return;
  const k = top === 'buy' ? 'recs' : (_lastReview || card.dataset.reviewOpen);
  const b = card.querySelector('.rtab[data-k="' + k + '"]') || card.querySelector('.rtab[data-top="' + top + '"]');
  if (b) _selectTab(b);
}}

function _selectTab(btn) {{
  if (btn.dataset.top) {{
    _syncTop(btn.dataset.top);
    if (btn.dataset.top === 'review') _lastReview = btn.dataset.k;
  }}
  document.querySelectorAll('.rtab').forEach(function(x){{ x.classList.toggle('active', x === btn); }});
  const key = btn.dataset.k;
  document.querySelectorAll('.rpanel').forEach(function(p){{ p.hidden = p.dataset.k !== key; }});
  _resetToolbar();
  _applyRecFilters();
}}

['#toolbar .fbtn', '#toolbar .cbtn'].forEach(function(sel){{
  document.querySelectorAll(sel).forEach(function(b){{
    b.addEventListener('click', function(){{
      document.querySelectorAll(sel).forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      _applyRecFilters();
    }});
  }});
}});
document.getElementById('recsearch').addEventListener('input', _applyRecFilters);

// 展開／收合一本書的決策過程：第一次點才向伺服器取，之後只切換顯示，不重取。
// 採購建議列用 data-rec 取 /trace/{{id}}；審核列用 data-ids（一組可含多個書目）取 /trace/pending?ids=…。
// 片段是伺服器端已逸出的 HTML，這裡只把它設成 innerHTML，不在前端拼資料。
function _traceUrl(tr) {{
  if (tr.dataset.rec) return '/trace/' + encodeURIComponent(tr.dataset.rec);
  return '/trace/pending?' + tr.dataset.ids.split(',')
    .map(function(i){{ return 'ids=' + encodeURIComponent(i); }}).join('&');
}}

async function _loadTrace(tr, cell) {{
  cell.textContent = '載入中…';
  try {{
    const r = await fetch(_traceUrl(tr));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    cell.innerHTML = await r.text();
  }} catch (e) {{
    cell.textContent = '載入失敗（' + e.message + '）　';
    const retry = document.createElement('button');
    retry.type = 'button'; retry.className = 'fbtn'; retry.textContent = '重試';
    retry.addEventListener('click', function(){{ _loadTrace(tr, cell); }});
    cell.appendChild(retry);
  }}
}}

function _onTraceClick(ev) {{
  const btn = ev.target.closest('.tbtn');
  if (!btn) return;
  const tr = btn.closest('tr');
  let row = tr.nextElementSibling;
  if (!row || !row.classList.contains('trace-row')) {{
    row = document.createElement('tr');
    row.className = 'trace-row';
    row.hidden = true;
    const td = document.createElement('td');
    td.colSpan = tr.children.length;      // 採購表 8 欄、審核表 6 欄
    row.appendChild(td);
    tr.after(row);
    _loadTrace(tr, td);
  }}
  const opening = row.hidden;
  row.hidden = !opening;
  btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
}}
// 採購表在審核卡片（#review）裡面，所以只在卡片上綁一次：兩邊都綁的話，一次點擊會觸發兩次（開了又立刻關）。
const _traceHost = document.getElementById('review');
if (_traceHost) _traceHost.addEventListener('click', _onTraceClick);

// 分頁切換
document.querySelectorAll('.rtab').forEach(function(b){{
  b.addEventListener('click', function(){{ _selectTab(b); }});
}});
_resetToolbar();      // 載入時讓工具列符合目前開著的分頁

// KPI「待人工審核」卡片：連結會捲到卡片，同時開啟待審最多的分頁
document.querySelectorAll('a.kpi[href="#review"]').forEach(function(a){{
  a.addEventListener('click', function(){{
    const card = document.getElementById('review');
    const tab = card && card.querySelector('.rtab[data-k="' + card.dataset.reviewOpen + '"]');
    if (tab) tab.click();
  }});
}});

// ---- 審核動作：決定、批次、撤銷、套用 ----
// 成功後整頁重新整理（記住目前的分頁與捲動位置）：待審清單、「審核紀錄」分頁與待套用數都由伺服器重算，
// 畫面不會與資料不一致。
function _reviewReload() {{
  const active = document.querySelector('.rtab.active');
  try {{
    if (active) sessionStorage.setItem('reviewTab', active.dataset.k);
    sessionStorage.setItem('reviewScroll', String(window.scrollY || 0));
  }} catch (e) {{}}
  // 不設 #review：那會讓瀏覽器跳回卡片頂端；捲動位置在載入後還原
  location.reload();
}}
try {{
  const k = sessionStorage.getItem('reviewTab');
  if (k) {{
    sessionStorage.removeItem('reviewTab');
    const b = document.querySelector('.rtab[data-k="' + k + '"]');
    if (b) b.click();
  }}
  const y = sessionStorage.getItem('reviewScroll');
  if (y !== null) {{
    sessionStorage.removeItem('reviewScroll');
    window.addEventListener('load', function(){{ window.scrollTo(0, Number(y) || 0); }});
  }}
}} catch (e) {{}}

async function _postJson(url, payload) {{
  const r = await fetch(url, {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)}});
  let data = null;
  try {{ data = await r.json(); }} catch (e) {{}}
  if (!r.ok || !data || data.ok === false) throw new Error((data && data.message) || ('HTTP ' + r.status));
  return data;
}}

function _rowIds(tr) {{ return tr.dataset.ids.split(',').map(Number); }}

function _rowBusy(tr, busy) {{
  tr.querySelectorAll('.abtn').forEach(function(b){{ b.disabled = busy; }});
}}

function _rowError(tr, message) {{
  const box = tr.querySelector('.aerr');
  if (box) {{ box.textContent = message; box.hidden = false; }}
}}

async function _decideRow(btn) {{
  const tr = btn.closest('tr');
  const payload = {{ids: _rowIds(tr), action: btn.dataset.act}};
  if (btn.dataset.act === 'custom') {{
    payload.title = tr.querySelector('.atitle').value;
    payload.isbn = tr.querySelector('.aisbn').value;
  }}
  _rowBusy(tr, true);
  try {{
    const d = await _postJson('/review/decide', payload);
    if (d.changed === 0) throw new Error('這組已被處理過，請重新整理頁面');
    _reviewReload();
  }} catch (e) {{
    _rowError(tr, e.message);
    _rowBusy(tr, false);
  }}
}}

function _visibleChecked(panel, selector) {{
  return Array.from(panel.querySelectorAll(selector + ':checked'))
    .map(function(c){{ return c.closest('tr'); }})
    .filter(function(tr){{ return tr.style.display !== 'none'; }});
}}

async function _undoRow(btn) {{
  const tr = btn.closest('tr');
  btn.disabled = true;
  try {{
    await _postJson('/review/undo', {{ids: _rowIds(tr)}});
    _reviewReload();
  }} catch (e) {{
    _rowError(tr, e.message);
    btn.disabled = false;
  }}
}}

async function _applyReview(btn) {{
  btn.disabled = true;
  try {{
    await _postJson('/review/apply', {{}});
    btn.textContent = '套用中…（進度見上方執行紀錄，完成後會自動重新整理）';
    // 與「執行 pipeline」相同：送出後要重啟狀態輪詢（閒置時它不會自己排程）。伺服器已確認工作開始，
    // 所以直接標記「親眼看過執行中」，即使工作在下一次輪詢前就結束，也一定會重新整理。
    _sawRunning = true;
    _poll();
  }} catch (e) {{
    alert(e.message);
    btn.disabled = false;
  }}
}}

const _reviewCard = document.getElementById('review');
if (_reviewCard) {{
  _reviewCard.addEventListener('click', function(ev){{
    const t = ev.target;
    const topb = t.closest('.toptab');
    if (topb) return _selectTop(topb.dataset.top);
    const goto = t.closest('[data-goto-top]');
    if (goto) return _selectTop(goto.dataset.gotoTop);
    const act = t.closest('[data-act]');
    if (act) return _decideRow(act);
    const fill = t.closest('[data-fill]');
    if (fill) {{                                  // 只填入書名，不送出
      fill.closest('tr').querySelector('.atitle').value = fill.dataset.fill;
      return;
    }}
    const undo = t.closest('[data-undo]');
    if (undo) return _undoRow(undo);
    const apply = t.closest('#applybtn');
    if (apply) return _applyReview(apply);
    const cart = t.closest('[data-cart]');
    if (cart) return _cartToggle(cart);
    const pbatch = t.closest('[data-pbatch]');
    if (pbatch) return _purchaseBatch(pbatch);
    const ubatch = t.closest('[data-ubatch]');
    if (ubatch) return _undoBatch(ubatch);
  }});
  _reviewCard.addEventListener('change', function(ev){{
    const all = ev.target.closest('.rchkall');
    if (all) all.closest('.rpanel').querySelectorAll('tbody tr:not(.trace-row)').forEach(function(tr){{
      const c = tr.querySelector('.rchk');
      if (c && tr.style.display !== 'none') c.checked = all.checked;     // 只勾看得見的列（搜尋／篩選後）
    }});
  }});
}}

// ---- 購物車與冊數決定 ----
// 決定冊數：留空＝還沒決定（不在購物車）、填 0＝這次不買、填大於 0＝加入購物車。
// 購物車本身在頁首的抽屜裡（整個購物車、全校）；這裡任何一列改變，就地更新該列與 KPI，並重取購物車更新圖示上的數字。
function _syncCartBtn(tr) {{
  const btn = tr.querySelector('[data-cart]');
  if (!btn) return;
  const inCart = tr.dataset.incart === '1';
  const sys = Number(tr.querySelector('td.num').textContent) || 0;
  btn.textContent = inCart ? '移出購物車' : '加入購物車';
  btn.disabled = !inCart && sys <= 0;
  btn.title = btn.disabled ? '系統建議為 0，請直接填冊數' : '';
}}

function _setRowState(tr, copies, final) {{
  const input = tr.querySelector('.dec-in');
  const saved = copies === null ? '' : String(copies);
  input.value = saved;
  input.dataset.saved = saved;
  input.classList.toggle('has', copies !== null);
  tr.dataset.final = final;
  if (copies !== null && copies > 0) tr.dataset.incart = '1'; else delete tr.dataset.incart;
  input.closest('td').removeAttribute('data-stale');
  _syncCartBtn(tr);
}}

// 同一列的動作排隊，一次只跑一個：輸入冊數會在按鈕被按下的瞬間先觸發 change（輸入框失焦），緊接著才是 click，
// 兩個請求若同時在飛，後到的會蓋掉先到的，畫面也會被先回來的那個回應改寫。排隊後，後面的動作看到的是前面存好的結果。
function _withRow(tr, fn) {{
  const input = tr.querySelector('.dec-in'), btn = tr.querySelector('[data-cart]');
  tr._busy = (tr._busy || 0) + 1;
  if (input) input.disabled = true;
  if (btn) btn.disabled = true;
  const run = (tr._chain || Promise.resolve()).then(fn);
  tr._chain = run.catch(function(){{}}).then(function(){{
    tr._busy -= 1;
    if (!tr._busy) {{
      if (input) input.disabled = false;
      _syncCartBtn(tr);
    }}
  }});
  return run;
}}

function _refreshCart() {{
  let books = 0, copies = 0;
  document.querySelectorAll('#rectable tbody tr:not(.trace-row)').forEach(function(tr){{
    copies += Number(tr.dataset.final) || 0;
    if (tr.dataset.incart === '1') books++;
  }});
  const set = function(id, v){{ const el = document.getElementById(id); if (el) el.textContent = v; }};
  set('kpi-final', copies);
  set('kpi-cart-books', books);
  _applyRecFilters();
  _loadCart();
}}

function _applyResults(results) {{
  document.querySelectorAll('#rectable .dec-in').forEach(function(input){{
    const r = results[input.dataset.key];
    if (r) _setRowState(input.closest('tr'), r.copies, r.final);
  }});
}}

const _recTable = document.getElementById('rectable');
if (_recTable) _recTable.addEventListener('change', function(ev){{
  const input = ev.target.closest('.dec-in');
  if (!input) return;
  const tr = input.closest('tr');
  const raw = input.value.trim();          // 在排隊前就讀：之後輸入框會被停用
  return _withRow(tr, async function(){{
    const err = tr.querySelector('.aerr');
    try {{
      const d = await _postJson('/purchase/decide', {{book_key: input.dataset.key, copies: raw === '' ? null : Number(raw)}});
      _setRowState(tr, d.copies, d.final);
      err.hidden = true;
      _refreshCart();
    }} catch (e) {{
      input.value = input.dataset.saved || '';
      err.textContent = e.message;
      err.hidden = false;
    }}
  }});
}});

// 單列「加入購物車／移出購物車」。加入＝輸入框裡有 ≥1 的冊數就用那個數字，沒有才採用伺服器端當下的系統建議；
// 移出＝清除決定。意圖在按下去那一刻決定；排在前面的儲存若已經把這本書加進購物車（使用者剛輸入的冊數），就不再覆蓋。
function _cartToggle(btn) {{
  const tr = btn.closest('tr');
  const adding = tr.dataset.incart !== '1';
  return _withRow(tr, async function(){{
    const input = tr.querySelector('.dec-in'), err = tr.querySelector('.aerr');
    try {{
      if (!adding) {{
        const d = await _postJson('/purchase/decide-many', {{book_keys: [input.dataset.key], mode: 'clear'}});
        _applyResults(d.results);
      }} else if (tr.dataset.incart !== '1') {{
        const typed = Number(input.value);
        if (typed >= 1) {{
          const d = await _postJson('/purchase/decide', {{book_key: input.dataset.key, copies: typed}});
          _setRowState(tr, d.copies, d.final);
        }} else {{
          const d = await _postJson('/purchase/decide-many', {{book_keys: [input.dataset.key], mode: 'suggested'}});
          if (d.skipped.length) throw new Error('系統建議為 0，請直接填冊數');
          _applyResults(d.results);
        }}
      }}
      err.hidden = true;
      _refreshCart();
    }} catch (e) {{
      err.textContent = e.message;
      err.hidden = false;
    }}
  }});
}}

// ---- 購物車抽屜（頁首左上角的購物車圖示）----
// 顯示整個購物車（全校，不隨學系篩選），資料來自 GET /cart；改冊數、移出、清空都沿用 /purchase/*，
// 並把結果同步回採購表上有出現的列。標題與書名一律用 textContent 放進去（不用 innerHTML）。
const _PLABEL = {_plabel_json};
const _SLABEL = {_slabel_json};
let _cartData = {{items: [], books: 0, copies: 0}};

function _renderCart(d) {{
  const list = document.getElementById('cartlist');
  const items = d.items || [];
  list.textContent = '';
  if (!items.length) {{
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = '購物車是空的。在採購建議的「採購冊數」填冊數，或按「加入購物車」。';
    list.appendChild(p);
  }}
  items.forEach(function(it){{
    const row = document.createElement('div');
    row.className = 'ci';
    row.dataset.key = it.book_key;
    const title = document.createElement('div');
    title.className = 'ci-title';
    title.textContent = it.title;
    const input = document.createElement('input');
    input.className = 'ci-in';
    input.type = 'number'; input.min = '0'; input.max = '9999'; input.step = '1';
    input.value = String(it.copies);
    input.dataset.saved = String(it.copies);
    input.setAttribute('aria-label', '採購冊數');
    const rm = document.createElement('button');
    rm.className = 'abtn'; rm.type = 'button'; rm.dataset.cartRemove = '1';
    rm.textContent = '移出';
    const sub = document.createElement('div');
    sub.className = 'ci-sub';
    sub.textContent = (_PLABEL[it.priority] || it.priority) + '・' + (_SLABEL[it.status] || it.status) + '・系統建議 ' + it.suggested + ' 冊';
    const err = document.createElement('div');
    err.className = 'aerr'; err.hidden = true;
    row.append(title, input, rm, sub, err);
    list.appendChild(row);
  }});
  document.getElementById('carttotal').textContent = (d.books || 0) + ' 本 · ' + (d.copies || 0) + ' 冊';
  document.getElementById('cartexport').disabled = !items.length;
  document.getElementById('cartclear').disabled = !items.length;
}}

async function _loadCart() {{
  try {{
    const r = await fetch('/cart');
    const d = await r.json();
    if (!r.ok || !d || d.ok === false || !Array.isArray(d.items)) return;
    _cartData = d;
    const badge = document.getElementById('cartcount');
    if (badge) {{ badge.textContent = d.books; badge.classList.toggle('zero', !d.books); }}
    if (!document.getElementById('cartdrawer').hidden) _renderCart(d);
  }} catch (e) {{}}
}}

function _openCart() {{
  document.getElementById('cartshade').hidden = false;
  document.getElementById('cartdrawer').hidden = false;
  document.getElementById('cartbtn').setAttribute('aria-expanded', 'true');
  _renderCart(_cartData);
  _loadCart();
  document.getElementById('cartclose').focus();
}}

function _closeCart() {{
  document.getElementById('cartshade').hidden = true;
  document.getElementById('cartdrawer').hidden = true;
  document.getElementById('cartbtn').setAttribute('aria-expanded', 'false');
  document.getElementById('cartbtn').focus();
}}

// 抽屜裡的變更：同一本書排隊（與採購表同一個道理），完成後同步採購表、KPI 與圖示
let _cartChain = Promise.resolve();
function _cartAct(row, fn) {{
  const els = row.querySelectorAll('input, button');
  els.forEach(function(e){{ e.disabled = true; }});
  const run = _cartChain.then(fn);
  _cartChain = run.catch(function(){{}});
  return run.catch(function(e){{
    const box = row.querySelector('.aerr');
    if (box) {{ box.textContent = e.message; box.hidden = false; }}
    els.forEach(function(x){{ x.disabled = false; }});
    const input = row.querySelector('.ci-in');
    if (input) input.value = input.dataset.saved || '';
  }});
}}

async function _cartSetCopies(row, input) {{
  const raw = input.value.trim();
  const d = await _postJson('/purchase/decide', {{book_key: row.dataset.key, copies: raw === '' ? null : Number(raw)}});
  const r = {{}}; r[row.dataset.key] = {{copies: d.copies, final: d.final}};
  _applyResults(r);
  _refreshCart();
}}

async function _cartRemove(row) {{
  const d = await _postJson('/purchase/decide-many', {{book_keys: [row.dataset.key], mode: 'clear'}});
  _applyResults(d.results);
  _refreshCart();
}}

async function _cartClear() {{
  const keys = (_cartData.items || []).map(function(i){{ return i.book_key; }});
  if (!keys.length) return;
  if (!confirm('確定清空整個購物車（' + keys.length + ' 本、' + _cartData.copies + ' 冊）？清空後這些書回到「尚未決定」。')) return;
  try {{
    for (let i = 0; i < keys.length; i += 200) {{      // 伺服器一次最多收 200 本
      const d = await _postJson('/purchase/decide-many', {{book_keys: keys.slice(i, i + 200), mode: 'clear'}});
      _applyResults(d.results);
    }}
  }} catch (e) {{
    alert(e.message);
  }}
  _refreshCart();
}}

function _exportCart() {{
  const items = _cartData.items || [];
  if (!items.length) {{ alert('購物車是空的'); return; }}
  const lines = [['書名', '採購冊數', '系統建議', '優先級', '館藏狀態'].map(_csvCell).join(',')];
  items.forEach(function(it){{
    lines.push([it.title, it.copies, it.suggested, _PLABEL[it.priority] || it.priority, _SLABEL[it.status] || it.status]
      .map(_csvCell).join(','));
  }});
  _downloadCsv(lines, 'cart');
}}

document.getElementById('cartbtn').addEventListener('click', _openCart);
document.getElementById('cartclose').addEventListener('click', _closeCart);
document.getElementById('cartshade').addEventListener('click', _closeCart);
document.addEventListener('keydown', function(ev){{
  if (ev.key === 'Escape' && !document.getElementById('cartdrawer').hidden) _closeCart();
}});
document.getElementById('cartexport').addEventListener('click', _exportCart);
document.getElementById('cartclear').addEventListener('click', _cartClear);
document.getElementById('cartlist').addEventListener('change', function(ev){{
  const input = ev.target.closest('.ci-in');
  if (!input) return;
  const row = input.closest('.ci');
  return _cartAct(row, function(){{ return _cartSetCopies(row, input); }});
}});
document.getElementById('cartlist').addEventListener('click', function(ev){{
  const rm = ev.target.closest('[data-cart-remove]');
  if (!rm) return;
  const row = rm.closest('.ci');
  return _cartAct(row, function(){{ return _cartRemove(row); }});
}});

// 採購建議的批次：勾選（只算看得見的列）→ 確認視窗 → 依 200 一批呼叫 /purchase/decide-many → 就地更新
async function _purchaseBatch(btn) {{
  const panel = btn.closest('.rpanel');
  const rows = _visibleChecked(panel, '.rowchk').filter(function(tr){{ return tr.querySelector('.dec-in'); }});
  if (!rows.length) {{ alert('請先勾選要處理的書'); return; }}
  if (!confirm('確定對 ' + rows.length + ' 本書執行「' + btn.textContent + '」？')) return;
  const mode = btn.dataset.pbatch;
  const keys = rows.map(function(tr){{ return tr.querySelector('.dec-in').dataset.key; }});
  const bar = panel.querySelectorAll('.rbatch .abtn');
  bar.forEach(function(b){{ b.disabled = true; }});
  let skipped = 0;
  try {{
    for (let i = 0; i < keys.length; i += 200) {{      // 伺服器一次最多收 200 本
      const d = await _postJson('/purchase/decide-many', {{book_keys: keys.slice(i, i + 200), mode: mode}});
      _applyResults(d.results);
      skipped += d.skipped.length;
    }}
  }} catch (e) {{
    alert(e.message);
  }}
  rows.forEach(function(tr){{ const c = tr.querySelector('.rowchk'); if (c) c.checked = false; }});
  const all = document.getElementById('rowchkall');
  if (all) all.checked = false;
  bar.forEach(function(b){{ b.disabled = false; }});
  _refreshCart();
  if (skipped) alert('略過 ' + skipped + ' 本（系統建議為 0，或找不到這本書的建議）');
}}

// 審核紀錄的批次撤銷：沿用 /review/undo（一次最多 200 個書目），完成後整頁重新整理
async function _undoBatch(btn) {{
  const panel = btn.closest('.rpanel');
  const rows = _visibleChecked(panel, '.rchk');
  if (!rows.length) {{ alert('請先勾選要撤銷的組'); return; }}
  const ids = [].concat.apply([], rows.map(_rowIds));
  if (!confirm('確定撤銷 ' + rows.length + ' 組（' + ids.length + ' 筆書目）的決定？')) return;
  panel.querySelectorAll('.rbatch .abtn').forEach(function(b){{ b.disabled = true; }});
  try {{
    for (let i = 0; i < ids.length; i += 200) {{
      await _postJson('/review/undo', {{ids: ids.slice(i, i + 200)}});
    }}
  }} catch (e) {{
    alert(e.message);
  }}
  _reviewReload();
}}

const _el = id => document.getElementById(id);
let _sawRunning = false;  // 這個分頁是否親眼看過 running；只有這樣才代表「這次是我觸發的」
async function _poll() {{
  try {{
    const d = await (await fetch('/status')).json();
    const c = d.counts;
    _el('counts').textContent =
      `課程 ${{c.courses}} · 書目 ${{c.citations}} · 已驗證 ${{c.verified}} · 館藏 ${{c.holdings}} · 建議 ${{c.recommendations}}`;
    const label = {{idle:'閒置中', running:`執行中… ${{d.elapsed||0}}s`, done:`完成（${{d.elapsed||0}}s）`, error:'錯誤：'+(d.error||'')}};
    _el('runstatus').textContent = label[d.status] || d.status;
    _el('runstatus').className = 'runstatus ' + d.status;
    _el('runbtn').disabled = (d.status === 'running');
    _logLoop = (d.status === 'running');  // 只在執行中輪詢 /log，閒置時不要讓終端機被 GET /log 洗版
    if (d.status === 'running') {{
      _sawRunning = true;
      _pollLog();
      setTimeout(_poll, 3000);
    }} else if (_sawRunning && (d.status === 'done' || d.status === 'error')) {{
      // 親眼看過它從 running 跑到這裡結束 → 整頁資料（KPI/圖表/建議明細）都舊了，重整一次
      location.reload();
    }}
  }} catch (e) {{ _el('runstatus').textContent = '無法連線'; }}
}}
// 更新「檔案」按鈕上的選取計數
function _updateFfLabel() {{
  const chks = Array.from(document.querySelectorAll('.ffchk'));
  const picked = chks.filter(c => c.checked).length;
  const txt = (chks.length === 0 || picked === chks.length) ? '全部' : `${{picked}}/${{chks.length}}`;
  _el('ffbtn').textContent = '檔案 (' + txt + ')';
}}

// 更新「學系」按鈕上的選取計數
function _updateDeptLabel() {{
  const chks = Array.from(document.querySelectorAll('.deptchk'));
  const picked = chks.filter(c => c.checked).length;
  const txt = (chks.length === 0 || picked === chks.length) ? '全部' : `${{picked}}/${{chks.length}}`;
  _el('deptbtn').textContent = '學系 (' + txt + ')';
}}

// ---- 學系篩選：選擇記在網址（?departments=甲&departments=乙），關閉面板時套用 ----
// 記在網址而不是只記在勾選框：pipeline 跑完頁面會自動 location.reload()，重整的就是這個網址，
// 篩選才不會掉回「全部」。用重複參數而不是逗號串接，系所名稱含逗號也不會切錯。
const _deptFromUrl = new URLSearchParams(location.search).getAll('departments');  // 載入時的篩選，空＝全部
let _navigate = function(url) {{ location.href = url; }};  // 獨立成函式，驗證時可以替換

function _deptUrl(names) {{
  if (!names.length) return location.pathname;   // 全部：網址保持乾淨
  const q = new URLSearchParams();
  names.forEach(n => q.append('departments', n));
  return location.pathname + '?' + q.toString();
}}

function _applyDeptFilter() {{
  // 選擇器沒載入成功（一個勾選框都沒有）：不動網址，否則一次網路錯誤就會悄悄清掉使用者的篩選
  if (!document.querySelector('.deptchk')) return;
  const picked = _collectChecked('.deptchk');            // null＝全部勾選；[]＝全不勾
  if (picked !== null && picked.length === 0) return;     // 全不勾：不導向（「執行」鈕另有擋）
  const now = picked === null ? [] : picked;
  const same = now.length === _deptFromUrl.length && now.every(d => _deptFromUrl.includes(d));
  if (same) return;                                       // 沒有變就不要重載
  _navigate(_deptUrl(now));
}}

// 載入「學院 → 系所」兩層樹，畫成可展開/勾選的清單（預設全勾；網址有篩選就只勾名單內的）
async function _loadDepts() {{
  try {{
    const d = await (await fetch('/departments')).json();
    const box = _el('deptfilter');
    if (!d.tree || !d.tree.length) {{
      box.innerHTML = '<span class="ff-empty">目前沒有系所資料</span>';
      _updateDeptLabel();
      return;
    }}
    // 每個學院一個區塊：學院列（含全選該院的框）＋ 底下系所勾選框
    const sections = d.tree.map(function(node){{
      const depts = node.depts.map(function(dep){{
        return `<label class="dept-item"><input type="checkbox" class="deptchk" value="${{dep}}" checked> ${{dep}}</label>`;
      }}).join('');
      return `<div class="dept-college"><label class="dept-col-lbl"><input type="checkbox" class="colchk" checked> <b>${{node.college}}</b></label>${{depts}}</div>`;
    }}).join('');
    box.innerHTML = '<div class="ff-actions"><a data-act="all">全選</a><a data-act="none">全不選</a></div>' + sections;
    // 系所勾選 → 更新計數
    box.querySelectorAll('.deptchk').forEach(c => c.addEventListener('change', _updateDeptLabel));
    // 學院框 → 連動該院底下所有系所
    box.querySelectorAll('.dept-college').forEach(function(sec){{
      const col = sec.querySelector('.colchk');
      const items = sec.querySelectorAll('.deptchk');
      col.addEventListener('change', function(){{
        items.forEach(c => {{ c.checked = col.checked; }});
        _updateDeptLabel();
      }});
      items.forEach(c => c.addEventListener('change', function(){{
        col.checked = Array.from(items).every(x => x.checked);
      }}));
    }});
    // 全選/全不選
    box.querySelectorAll('.ff-actions a').forEach(a => a.addEventListener('click', function(){{
      const on = a.dataset.act === 'all';
      box.querySelectorAll('.deptchk, .colchk').forEach(c => {{ c.checked = on; }});
      _updateDeptLabel();
    }}));
    // 從網址還原勾選：沒有 departments 參數＝維持預設全勾；有就只勾名單內的，學院框跟著重算
    if (_deptFromUrl.length) {{
      box.querySelectorAll('.deptchk').forEach(c => {{ c.checked = _deptFromUrl.includes(c.value); }});
      box.querySelectorAll('.dept-college').forEach(function(sec){{
        sec.querySelector('.colchk').checked =
          Array.from(sec.querySelectorAll('.deptchk')).every(x => x.checked);
      }});
    }}
    _updateDeptLabel();
  }} catch (e) {{ _el('deptfilter').innerHTML = '<span class="ff-empty">無法載入系所清單</span>'; }}
}}
_el('deptbtn').addEventListener('click', function(ev){{
  ev.stopPropagation();
  const panel = _el('deptfilter');
  panel.hidden = !panel.hidden;
  if (panel.hidden) _applyDeptFilter();   // 按按鈕關閉面板也算「關閉」
}});
document.addEventListener('click', function(ev){{
  const panel = _el('deptfilter');
  if (!panel.hidden && !panel.contains(ev.target) && ev.target !== _el('deptbtn')) {{
    panel.hidden = true;
    _applyDeptFilter();   // 點面板外關閉：選擇有變才導向
  }}
}});

// 載入 data/ 的 xlsx 清單，畫成勾選框（預設全勾）
async function _loadFiles() {{
  try {{
    const d = await (await fetch('/files')).json();
    const box = _el('filefilter');
    if (!d.files || !d.files.length) {{
      box.innerHTML = '<span class="ff-empty">data/ 目前沒有 xlsx</span>';
      _updateFfLabel();
      return;
    }}
    box.innerHTML =
      '<div class="ff-actions"><a data-act="all">全選</a><a data-act="none">全不選</a></div>' +
      d.files.map(function(f){{
        return `<label><input type="checkbox" class="ffchk" value="${{f}}" checked> ${{f}}</label>`;
      }}).join('');
    box.querySelectorAll('.ffchk').forEach(c => c.addEventListener('change', _updateFfLabel));
    box.querySelectorAll('.ff-actions a').forEach(a => a.addEventListener('click', function(){{
      const on = a.dataset.act === 'all';
      box.querySelectorAll('.ffchk').forEach(c => {{ c.checked = on; }});
      _updateFfLabel();
    }}));
    _updateFfLabel();
  }} catch (e) {{ _el('filefilter').innerHTML = '<span class="ff-empty">無法載入檔案清單</span>'; }}
}}

// 「篩選」按鈕：點一下開/關下拉面板；點面板外部收起
_el('ffbtn').addEventListener('click', function(ev){{
  ev.stopPropagation();
  _el('filefilter').hidden = !_el('filefilter').hidden;
}});
document.addEventListener('click', function(ev){{
  const panel = _el('filefilter');
  if (!panel.hidden && !panel.contains(ev.target) && ev.target !== _el('ffbtn')) {{
    panel.hidden = true;
  }}
}});

_el('fileupload').onchange = async (ev) => {{
  const files = Array.from(ev.target.files);
  if (!files.length) return;
  const failed = [];
  for (const file of files) {{      // 逐一上傳（/upload 一次收一個檔）
    const fd = new FormData();
    fd.append('file', file);
    const r = await (await fetch('/upload', {{method:'POST', body: fd}})).json();
    if (!r.ok) failed.push(file.name + '：' + (r.message || '上傳失敗'));
  }}
  if (failed.length) alert('部分檔案上傳失敗：\\n' + failed.join('\\n'));
  ev.target.value = '';       // 清掉，讓同一批檔可以再次觸發 onchange
  await _loadFiles();          // 重新載入清單，新檔預設會被勾選
}};

// 從一組勾選框收集選取值；全勾（或沒有勾選框）＝不限定，回傳 null
function _collectChecked(sel) {{
  const chks = Array.from(document.querySelectorAll(sel));
  const picked = chks.filter(c => c.checked).map(c => c.value);
  const allChecked = chks.length > 0 && picked.length === chks.length;
  return (chks.length === 0 || allChecked) ? null : picked;
}}

_el('runbtn').onclick = async () => {{
  const source_files = _collectChecked('.ffchk');
  const departments = _collectChecked('.deptchk');
  if (source_files && source_files.length === 0) {{ alert('請至少勾選一個檔案，或全部勾選代表全跑'); return; }}
  if (departments && departments.length === 0) {{ alert('請至少勾選一個學系，或全部勾選代表全跑'); return; }}
  const scopeMsg = departments ? `選中 ${{departments.length}} 個學系` : '全部學系';
  if (!confirm(`即將對「${{scopeMsg}}」執行 pipeline，可能需要一段時間。確定要執行嗎？`)) return;
  _el('runbtn').disabled = true;
  await fetch('/run', {{
    method:'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{source_files: source_files, departments: departments}}),
  }});
  _poll();
}};

// ---- 匯出 CSV：下拉面板 + 前端組 CSV ----
_el('expbtn').addEventListener('click', function(ev){{
  ev.stopPropagation();
  _el('exppanel').hidden = !_el('exppanel').hidden;
}});
document.addEventListener('click', function(ev){{
  const p = _el('exppanel');
  if (!p.hidden && !p.contains(ev.target) && ev.target !== _el('expbtn')) p.hidden = true;
}});

// 表頭全選框：勾選/取消目前「可見」的列（配合優先級篩選）
const _chkall = _el('rowchkall');
if (_chkall) _chkall.addEventListener('change', function(){{
  document.querySelectorAll('#rectable tbody tr:not(.trace-row)').forEach(function(tr){{
    if (tr.style.display !== 'none') {{
      const c = tr.querySelector('.rowchk');
      if (c) c.checked = _chkall.checked;
    }}
  }});
}});

function _csvCell(s) {{
  s = (s == null ? '' : String(s)).replace(/\\s+/g, ' ').trim();
  return /[",\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}}

function _downloadCsv(lines, prefix) {{
  const blob = new Blob(['\\ufeff' + lines.join('\\r\\n')], {{type:'text/csv;charset=utf-8'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const stamp = new Date().toISOString().slice(0,10).replace(/-/g,'');
  a.href = url; a.download = prefix + '_' + stamp + '.csv';
  a.click();
  URL.revokeObjectURL(url);
}}

_el('expgo').addEventListener('click', function(){{
  const panel = _activePanel();
  const table = panel ? panel.querySelector('table') : null;
  if (!table) {{ alert('沒有符合條件的資料可匯出'); return; }}
  const key = _activeKey(), kind = panel.dataset.export;
  const onlyChecked = _el('exponlychecked').checked;
  const usesPrio = (panel.dataset.filters || '').split(' ').includes('prio');
  const wantPrio = new Set(Array.from(document.querySelectorAll('.expchk'))
    .filter(c => c.checked).map(c => c.value));
  let header, cellsOf, prefix;
  if (kind === 'recs') {{
    prefix = 'recommendations';
    header = ['優先級','課程','書名','類別','館藏狀態','冊數','需求冊數','決定冊數','最終採購數','理由'];
    // 決定冊數是輸入框、「需求」標記由 CSS 畫，兩者都不在 textContent 裡，所以明確讀 data-need 與輸入框的值；
    // 最終採購數＝決定冊數，沒決定是 0（沒加入購物車就不算要買）。
    cellsOf = function(tr) {{
      const numCell = tr.querySelector('td.num');
      const input = tr.querySelector('.dec-in');
      const copies = numCell.textContent.trim();
      const decided = input ? input.value.trim() : '';
      return Array.from(tr.querySelectorAll('td')).slice(1, 6).map(td => td.textContent)
        .concat([copies, numCell.dataset.need || '', decided, decided !== '' ? decided : '0',
                 tr.querySelector('td.rationale').textContent]);
    }};
  }} else if (kind === 'review') {{
    prefix = 'review_' + key;
    header = ['優先級','課程','書名','配到的書','原因','人數合計'];
    cellsOf = function(tr) {{
      const td = tr.querySelectorAll('td');
      const text = (cell, sel) => (cell.querySelector(sel) || cell).textContent;
      return [td[1].textContent, td[2].textContent, text(td[3], '.bt'), text(td[4], '.bm'), td[5].textContent, td[6].textContent];
    }};
  }} else {{
    prefix = 'review_log';
    header = ['課程','書名','決定','採購書名','狀態'];
    cellsOf = function(tr) {{
      const td = tr.querySelectorAll('td');
      return [td[1].textContent, (td[2].querySelector('.bt') || td[2]).textContent, td[3].textContent, td[4].textContent, td[5].textContent];
    }};
  }}
  const lines = [header.map(_csvCell).join(',')];
  let n = 0;
  table.querySelectorAll('tbody tr:not(.trace-row)').forEach(function(tr){{
    if (onlyChecked) {{
      const c = tr.querySelector('.rowchk, .rchk');
      if (!c || !c.checked) return;
    }} else if (usesPrio && tr.dataset.priority !== 'none' && !wantPrio.has(tr.dataset.priority)) {{
      return;      // 不在採購清單的列（優先級 none）不受優先級勾選項影響
    }}
    lines.push(cellsOf(tr).map(_csvCell).join(','));
    n++;
  }});
  if (n === 0) {{ alert('沒有符合條件的資料可匯出'); return; }}
  _downloadCsv(lines, prefix);
  _el('exppanel').hidden = true;
}});


_loadFiles();
_loadDepts();
_poll();
_pollLog();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(departments: list[str] | None = Query(None)) -> str:
    return _render(_load_data(departments))


@app.get("/trace/pending", response_class=HTMLResponse)
def trace_pending(ids: list[int] | None = Query(None)):
    """待審核的書（還沒有採購建議）與審核紀錄（已做過決定）的追溯片段；一組可含多個書目（ids 重複帶）。
    沒有 id 或超過 50 個回 400（非數字由 FastAPI 回 422）；全部都既非待審也沒有決定紀錄回 404。"""
    from library_agent.trace_view import build_pending_trace, render_trace

    if not ids or len(ids) > 50:
        return HTMLResponse("請提供 1 到 50 個待審核書目的 id。", status_code=400)
    with SessionLocal() as s:
        result = build_pending_trace(s, ids)
    if result is None:
        return HTMLResponse("找不到待審核或已決定的書目。", status_code=404)
    return HTMLResponse(render_trace(result))


@app.get("/trace/{rec_id}", response_class=HTMLResponse)
def trace(rec_id: int):
    """一本書的決策過程（HTML 片段，內容已逸出）。前端第一次展開該列時才取。"""
    from library_agent.trace_view import build_trace, render_trace   # 延後 import：它會載入 recommender

    with SessionLocal() as s:
        result = build_trace(s, rec_id)
    if result is None:
        return HTMLResponse("找不到這筆採購建議。", status_code=404)
    return HTMLResponse(render_trace(result))


# ---------- CSV 匯出 ----------

def _csv_response(header: list[str], rows: list[tuple], filename_prefix: str) -> StreamingResponse:
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM，讓 Excel 開啟中文不會亂碼
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"{filename_prefix}_{stamp}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/recommendations")
def export_recommendations() -> StreamingResponse:
    data = _load_data()
    header = ["優先級", "課程", "書名", "類別", "館藏狀態", "冊數", "需求冊數", "決定冊數", "最終採購數", "理由"]
    rows = [
        (
            _PRIORITY_LABEL.get(r.priority, r.priority),
            "、".join(c.name for c in r.courses),
            r.title,
            "、".join(_KEY_LABEL[k] for k in sorted({c.key for c in r.courses}, key=_KEY_RANK.get)),
            _STATUS_LABEL.get(r.status, r.status),
            r.copies,
            r.need,
            "" if r.decided is None else r.decided,
            r.final,
            r.rationale,
        )
        for r in data["recs"]
    ]
    return _csv_response(header, rows, "recommendations")


@app.get("/export/pending")
def export_pending() -> StreamingResponse:
    data = _load_data()
    header = ["課程", "書名", "類別", "confidence", "來源"]
    rows = [
        (
            course,
            title,
            "AI推薦" if _is_ai_recommended(raw_mention) else ("指定" if is_required else "參考"),
            confidence,
            source,
        )
        for course, title, confidence, source, is_required, raw_mention, _vb_id in data["pending"]
    ]
    return _csv_response(header, rows, "pending_review")


# ---------- 執行 pipeline + 進度 ----------

_run_lock = threading.Lock()
_run_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "error": None, "run_id": None}


def _run_pipeline(run_id: str, source_files: list[str] | None, departments: list[str] | None) -> None:
    from library_agent.graph import build_graph
    initial: dict = {}
    if source_files:
        initial["source_files"] = source_files
    if departments:
        initial["departments"] = departments
    runlog.reset()
    runlog.install()  # 把 print()/stderr 多存一份給網頁的執行紀錄；終端機輸出不變
    try:
        build_graph(run_id).invoke(initial)
        status, error = "done", None
    except Exception as e:  # 背景執行緒的例外要自己接，否則靜默消失
        traceback.print_exc()  # 走 stderr：終端機與網頁 log 都看得到錯誤出在哪
        status, error = "error", str(e)[:500]
    finally:
        runlog.uninstall()  # 成功或失敗都要還原，不能讓 stdout 永遠被換掉
    with _run_lock:
        _run_state.update(status=status, finished_at=time.time(), error=error)


# 系所名 → 對應的所有原始系級值（供 /run 把前端選的系所展開成 crawler 能比對的值）
_DEPT_TO_RAW: dict[str, list[str]] = {}
for _raw, (_col, _dep) in DEPARTMENT_MAP.items():
    _DEPT_TO_RAW.setdefault(_dep, []).append(_raw)


def _expand_departments(names: list[str] | None) -> list[str] | None:
    """系所名（UI 上看到、/departments 回傳的名稱）→ 展平成原始系級值，供 SQL 比對 Course.department。
    None 或空清單＝不篩（回 None）。未知的系所名原樣保留，例如「其他」歸類的原始值本身。
    /run（決定分析哪些課）與首頁（決定顯示哪些資料）共用，兩邊的「學系」才會是同一個意思。"""
    if not names:
        return None
    expanded: list[str] = []
    for name in names:
        expanded.extend(_DEPT_TO_RAW.get(name, [name]))
    return expanded


def _review_ids(body) -> list | None:
    """body["ids"] 必須是整數清單（布林不算）；否則回 None。個數與內容的其他檢查在 review_actions。"""
    ids = body.get("ids") if isinstance(body, dict) else None
    if isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return ids
    return None


def _review_error(message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=400)


@app.post("/review/decide")
def review_decide(body: dict = Body(...)):
    """對一組待審書目下決定（買配對到的書／買原書／輸入書名加入／都不買）。只收 JSON；驗證失敗不寫入任何東西。"""
    ids = _review_ids(body)
    if ids is None:
        return _review_error("ids 必須是整數清單")
    title, isbn = body.get("title"), body.get("isbn")
    if not all(v is None or isinstance(v, str) for v in (title, isbn)):
        return _review_error("書名與 ISBN 必須是文字")
    try:
        with SessionLocal() as s:
            result = review_actions.decide(s, ids, body.get("action"), title=title, isbn=isbn)
            s.commit()
    except review_actions.ReviewError as e:
        return _review_error(str(e))
    return {"ok": True, "changed": len(result.changed), "skipped": result.skipped}


@app.post("/review/undo")
def review_undo(body: dict = Body(...)):
    """撤銷決定：改回待審，清掉選擇與採購書名。"""
    ids = _review_ids(body)
    if ids is None:
        return _review_error("ids 必須是整數清單")
    try:
        with SessionLocal() as s:
            result = review_actions.undo(s, ids)
            s.commit()
    except review_actions.ReviewError as e:
        return _review_error(str(e))
    return {"ok": True, "changed": len(result.changed), "skipped": result.skipped}


_MAX_DECIDED_COPIES = 9999


@app.get("/cart")
def cart() -> dict:
    """整個購物車（全校）給頁首購物車抽屜；打開抽屜時才取。"""
    with SessionLocal() as s:
        items = _load_cart(s)
    return {"ok": True, "items": items, "books": len(items), "copies": sum(i["copies"] for i in items)}


@app.post("/purchase/decide")
def purchase_decide(body: dict = Body(...)):
    """記錄人對一本書採購冊數的決定。copies 為整數 0–9999（0＝這次不買，書仍留在清單）；
    null／省略＝清除決定（回到「還沒決定」，不在購物車）。決定以 book_key 對應，存在獨立的 purchase_decisions，
    recommendations 被刪掉重建也不會消失。只收 JSON；驗證失敗回 400 且不寫入。"""
    key = body.get("book_key") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key.strip():
        return _review_error("book_key 必須是非空的文字")
    key = key.strip()
    copies = body.get("copies")
    if copies is not None and (isinstance(copies, bool) or not isinstance(copies, int)
                               or not 0 <= copies <= _MAX_DECIDED_COPIES):
        return _review_error(f"冊數必須是 0 到 {_MAX_DECIDED_COPIES} 的整數（留空＝採用系統建議）")

    with SessionLocal() as s:
        suggested = s.scalar(select(Recommendation.suggested_copies).where(Recommendation.book_key == key))
        if copies is None:             # 清除：對應的建議已不存在也要能清掉
            s.execute(delete(PurchaseDecision).where(PurchaseDecision.book_key == key))
            s.commit()
            return {"ok": True, "copies": None, "suggested": suggested, "final": 0}     # 沒決定＝不在購物車
        if suggested is None:
            return JSONResponse({"ok": False, "message": "找不到這本書的採購建議（可能已被重算移除，請重新整理頁面）"},
                                status_code=404)
        now = datetime.now(timezone.utc)
        s.execute(
            pg_insert(PurchaseDecision)
            .values(book_key=key, copies=copies, suggested_at_decision=suggested, decided_at=now)
            .on_conflict_do_update(index_elements=["book_key"], set_={
                "copies": copies, "suggested_at_decision": suggested, "decided_at": now})
        )
        s.commit()
    return {"ok": True, "copies": copies, "suggested": suggested, "final": copies}


_BATCH_MODES = ("suggested", "zero", "clear")
_MAX_BATCH = 200


@app.post("/purchase/decide-many")
def purchase_decide_many(body: dict = Body(...)):
    """批次決定（採購建議／購物車分頁的批次按鈕）。mode：
      suggested  決定冊數＝伺服器端「當下」的系統建議（＝加入購物車，記下決定當時的建議）；系統建議為 0 的略過
      zero       決定冊數＝0（這次不買，明確排除）
      clear      刪除決定（回到還沒決定、不在購物車；對應的建議已不存在也要能清掉）
    一次最多 200 本。驗證失敗回 400 且不寫入任何東西；找不到建議的 key 略過（clear 除外），不讓整批失敗。
    回傳 results：每個 key 處理後的決定冊數與最終冊數，前端據此就地更新各列。"""
    keys = body.get("book_keys") if isinstance(body, dict) else None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in _BATCH_MODES:
        return _review_error("mode 必須是 suggested、zero 或 clear")
    if (not isinstance(keys, list) or not keys or len(keys) > _MAX_BATCH
            or not all(isinstance(k, str) and k.strip() for k in keys)):
        return _review_error(f"book_keys 必須是 1 到 {_MAX_BATCH} 個非空文字")
    keys = list(dict.fromkeys(k.strip() for k in keys))      # 去重並保持順序

    with SessionLocal() as s:
        if mode == "clear":
            deleted = s.execute(delete(PurchaseDecision).where(PurchaseDecision.book_key.in_(keys))).rowcount
            s.commit()
            return {"ok": True, "changed": deleted, "skipped": [],
                    "results": {k: {"copies": None, "final": 0} for k in keys}}
        suggestions = dict(s.execute(
            select(Recommendation.book_key, Recommendation.suggested_copies).where(Recommendation.book_key.in_(keys))
        ).all())
        now = datetime.now(timezone.utc)
        values, results, skipped = [], {}, []
        for k in keys:
            suggested = suggestions.get(k)
            if suggested is None or (mode == "suggested" and suggested == 0):
                skipped.append(k)                 # 找不到建議，或沒有東西可加（系統建議為 0）
                continue
            copies = suggested if mode == "suggested" else 0
            values.append({"book_key": k, "copies": copies, "suggested_at_decision": suggested, "decided_at": now})
            results[k] = {"copies": copies, "final": copies}
        if values:
            stmt = pg_insert(PurchaseDecision).values(values)
            s.execute(stmt.on_conflict_do_update(index_elements=["book_key"], set_={
                "copies": stmt.excluded.copies, "suggested_at_decision": stmt.excluded.suggested_at_decision,
                "decided_at": stmt.excluded.decided_at}))
        s.commit()
    return {"ok": True, "changed": len(values), "skipped": skipped, "results": results}


def _run_apply() -> None:
    """背景執行「套用審核結果」。與 pipeline 共用 _run_state／_run_lock 與網頁執行紀錄：同時只能有一個在跑，
    結束後頁面既有的輪詢會自動重整。"""
    from library_agent.review_apply import apply_review
    runlog.reset()
    runlog.install()
    try:
        apply_review()
        status, error = "done", None
    except Exception as e:  # 背景執行緒的例外要自己接，否則靜默消失
        traceback.print_exc()
        status, error = "error", str(e)[:500]
    finally:
        runlog.uninstall()
    with _run_lock:
        _run_state.update(status=status, finished_at=time.time(), error=error)


@app.post("/review/apply")
def review_apply_start() -> dict:
    """把審核決定反映到館藏與採購建議（重查館藏＋重算採購建議），背景執行。"""
    from library_agent.review_apply import pending_apply_ids

    with _run_lock:
        if _run_state["status"] == "running":
            return {"ok": False, "message": "有工作正在執行中，請稍後再試"}
        with SessionLocal() as s:
            count = len(pending_apply_ids(s))
        if count == 0:
            return {"ok": False, "message": "沒有待套用的審核結果"}
        _run_state.update(status="running", started_at=time.time(), finished_at=None, error=None, run_id=None)
    threading.Thread(target=_run_apply, daemon=True).start()
    return {"ok": True, "count": count}


@app.post("/run")
def start_run(body: dict = Body(default={})) -> dict:
    """在背景執行緒啟動整條 pipeline。source_files/departments 為 None/空＝全部；一次只准一個。
    departments 收到的是「系所名」，在此展開成 crawler 比對用的原始系級值。"""
    from library_agent.graph import _init_run

    source_files = body.get("source_files") if isinstance(body, dict) else None
    dept_names = body.get("departments") if isinstance(body, dict) else None
    departments = _expand_departments(dept_names)
    with _run_lock:
        if _run_state["status"] == "running":
            return {"ok": False, "message": "pipeline 已在執行中"}
        run_id = str(time.time())
        _init_run(run_id)
        _run_state.update(status="running", started_at=time.time(), finished_at=None, error=None, run_id=run_id)
    runlog.reset()
    threading.Thread(target=_run_pipeline, args=(run_id, source_files, departments), daemon=True).start()
    return {"ok": True}


# ---------- xlsx 檔案 / 系所 管理 ----------

@app.get("/files")
def list_files() -> dict:
    """列出 data/ 目錄現有的 xlsx 檔名，供前端篩選 UI 用。"""
    names = sorted(p.name for p in DATA_DIR.glob("*.xlsx"))
    return {"files": names}


@app.get("/departments")
def list_departments() -> dict:
    """回傳「學院 → 系所」兩層樹，只列 DB 裡實際有課的系所（依 DEPARTMENT_MAP 歸類）。
    格式：{"tree": [{"college": 學院, "depts": [系所名, ...]}, ...]}。"""
    with SessionLocal() as s:
        raw_depts = s.scalars(
            select(Course.department).where(Course.department.is_not(None)).distinct()
        ).all()
    # 原始系級值 → (學院, 系所)；未知的歸到 (其他, 原始值)
    tree: dict[str, set[str]] = {}
    for raw in raw_depts:
        college, dept = DEPARTMENT_MAP.get(raw, ("其他", raw))
        tree.setdefault(college, set()).add(dept)
    return {"tree": [{"college": c, "depts": sorted(tree[c])} for c in sorted(tree)]}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict:
    """接收上傳的 xlsx 存到 data/（同名覆蓋）。只接受 .xlsx。"""
    name = Path(file.filename or "").name  # 去掉任何目錄成分，防路徑穿越
    if not name.lower().endswith(".xlsx"):
        return {"ok": False, "message": "只接受 .xlsx 檔"}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / name
    with open(dest, "wb") as f:
        f.write(await file.read())
    return {"ok": True, "filename": name}


@app.get("/log")
def get_log(after: int = 0, epoch: int | None = None) -> dict:
    """執行紀錄（終端文字）。after=上次拿到的流水號；epoch 與目前不同（新的一次執行）就整份重給。"""
    return runlog.read(after, epoch)


@app.get("/status")
def status() -> dict:
    """回傳執行狀態 + 各表即時筆數（agent 邊跑邊寫，筆數就邊長 → 當進度用）+ 各節點狀態。"""
    from library_agent.db.models import PipelineNodeRun
    from library_agent.graph import NODE_NAMES

    with _run_lock:
        st = dict(_run_state)
    with SessionLocal() as s:
        counts = {
            "courses": s.scalar(select(func.count()).select_from(Course)) or 0,
            "citations": s.scalar(select(func.count()).select_from(Citation)) or 0,
            "verified": s.scalar(
                select(func.count()).select_from(VerifiedBook).where(VerifiedBook.verified.is_(True))
            ) or 0,
            "holdings": s.scalar(select(func.count()).select_from(HoldingCheck)) or 0,
            "recommendations": s.scalar(select(func.count()).select_from(Recommendation)) or 0,
        }
        node_rows = {}
        if st["run_id"]:
            rows = s.execute(
                select(PipelineNodeRun.node_name, PipelineNodeRun.status)
                .where(PipelineNodeRun.run_id == st["run_id"])
                .order_by(PipelineNodeRun.id.desc())
            ).all()
            for node_name, node_status in rows:
                if node_name not in node_rows:  # 每個節點只取最新一筆（id desc 已排序）
                    node_rows[node_name] = node_status

    nodes = []
    for name in NODE_NAMES:
        node_status = node_rows.get(name, "pending")
        nodes.append({"name": name, "status": node_status})

    elapsed = None
    if st["started_at"]:
        elapsed = round((st["finished_at"] or time.time()) - st["started_at"])
    return {"status": st["status"], "elapsed": elapsed, "error": st["error"], "counts": counts, "nodes": nodes}
