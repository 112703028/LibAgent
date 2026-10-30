"""單筆追溯：一本書的採購建議是怎麼來的。

五段：課綱原文 → 解析結果 → 驗證配對 → Alma 館藏 → 冊數怎麼算。
build_trace 蒐集資料（只讀），render_trace 產生已逸出的 HTML 片段。

課綱、書名、作者都是第三方文字，進 HTML 一律 html.escape；前端只把這份已逸出的片段設成 innerHTML。
冊數怎麼算用 recommender.merge_group 重算（與 recommender 同一套規則），並與資料庫存的建議比對：
不一致時要明確標出，而不是默默顯示不同的數字。
"""
import html
import json
import math
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from library_agent.book_kind import book_type_key
from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, RecommendationCourse, VerifiedBook,
)
from library_agent.state import HoldingStatus

MAX_SYLLABUS = 6000   # 完整課綱最多顯示幾個字元（實測中位數 207 字，但最長 32454 字）


@dataclass(frozen=True)
class Syllabus:
    course_id: str
    course_name: str
    semester: str
    text: str
    length: int            # 原長度（text 可能被截斷）


@dataclass(frozen=True)
class SourceItem:
    """這本書在某門課裡的一筆書目（第 1、2 段）。"""
    course_id: str
    course_name: str
    citation_id: int
    mention: str           # 書目被提到的那一段
    title: str
    authors: list
    edition: str | None
    isbn: str | None
    publisher: str | None
    year: int | None
    kind: str              # required / optional / ai
    confidence: float
    enrolled: int          # 計算當時該課的人數（recommendation_courses 的快照）


@dataclass(frozen=True)
class Pairing:
    """原書配到哪本權威記錄、稽核怎麼判（第 3 段）。結果完全相同的書目合併成一項。"""
    matched_title: str
    matched_authors: list
    isbn: str | None
    source: str | None
    verdict: str | None    # same / mismatch / None（尚未稽核）
    judge: str | None      # rule / llm
    reason: str | None
    review_status: str | None
    originals: list        # [(原書名, 原作者 list), ...]


@dataclass(frozen=True)
class HoldingInfo:
    status: str
    copies: int
    mms_id: str | None
    edition: str | None
    members_differ: bool   # 各書目的館藏查詢結果不一致


@dataclass(frozen=True)
class Calc:
    courses: list          # [(課名, 人數)]，每門課只列一次
    enrolled_total: int
    per_copy: int
    need: int
    current: int
    gap: int
    cap: int
    is_required: bool
    status: str
    priority: str
    copies: int
    stored_priority: str
    stored_copies: int
    consistent: bool


@dataclass(frozen=True)
class Trace:
    rec_id: int
    title: str
    book_key: str | None
    rationale: str | None
    syllabi: list
    sources: list
    pairings: list
    holding: HoldingInfo | None
    calc: Calc | None


def _authors(raw: str | None) -> list:
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def _calc(members: list, stored_priority: str, stored_copies: int) -> Calc:
    """用 merge_group 重算，並拆出畫面要解釋的中間值（需求、缺口）。
    need 的算法必須與 recommender._decide 相同：總人數 0 時需求為 1。"""
    from library_agent.agents import recommender as R   # 延後 import：它在 import 時會建構 OpenAI client

    decision = R.merge_group(members)
    per = max(R._settings.students_per_copy, 1)
    per_course: dict[str, tuple[str, int]] = {}
    for m in sorted(members, key=lambda m: (m.course_name, m.course_id)):
        per_course.setdefault(m.course_id, (m.course_name, m.enrolled))
    total = decision.enrolled_total
    need = math.ceil(total / per) if total > 0 else 1
    return Calc(
        courses=list(per_course.values()), enrolled_total=total, per_copy=per, need=need,
        current=decision.current_copies, gap=max(need - decision.current_copies, 0),
        cap=R._settings.max_purchase_copies, is_required=decision.is_required,
        status=decision.status.value, priority=decision.priority.value, copies=decision.copies,
        stored_priority=stored_priority, stored_copies=stored_copies,
        consistent=(decision.priority.value, decision.copies) == (stored_priority, stored_copies),
    )


def build_trace(session, rec_id: int) -> Trace | None:
    """蒐集一筆採購建議的決策過程。找不到回 None。"""
    from library_agent.agents import recommender as R

    rec = session.get(Recommendation, rec_id)
    if rec is None:
        return None

    links = session.execute(
        select(RecommendationCourse).where(RecommendationCourse.recommendation_id == rec_id)
    ).scalars().all()
    snapshot = {link.citation_id: link.enrolled for link in links}     # 計算當時的人數
    if links:
        citation_ids = list(snapshot)
    else:   # 舊格式（沒有關聯列）：退回這筆建議自己的書目
        own = session.scalar(
            select(VerifiedBook.citation_id)
            .join(HoldingCheck, HoldingCheck.verified_book_id == VerifiedBook.id)
            .where(HoldingCheck.id == rec.holding_id)
        )
        citation_ids = [own] if own is not None else []

    rows = session.execute(
        select(Citation, VerifiedBook, HoldingCheck, Course)
        .join(VerifiedBook, VerifiedBook.citation_id == Citation.id)
        .outerjoin(HoldingCheck, HoldingCheck.verified_book_id == VerifiedBook.id)
        .join(Course, Course.course_id == Citation.course_id)
        .where(Citation.id.in_(citation_ids))
        .order_by(Course.course_name, Citation.id)
    ).all()

    syllabi: dict[str, Syllabus] = {}
    sources: list[SourceItem] = []
    grouped: dict[tuple, list] = {}
    members = []
    holdings = []
    for citation, vb, holding, course in rows:
        enrolled = snapshot.get(citation.id, course.enrolled_count or 0)
        syllabi.setdefault(course.course_id, Syllabus(
            course.course_id, course.course_name, course.semester,
            (course.raw_content or "")[:MAX_SYLLABUS], len(course.raw_content or ""),
        ))
        sources.append(SourceItem(
            course.course_id, course.course_name, citation.id, citation.raw_mention or "",
            citation.title, _authors(citation.authors), citation.edition, citation.isbn,
            citation.publisher, citation.year, book_type_key(citation.is_required, citation.raw_mention),
            citation.confidence or 0.0, enrolled,
        ))
        key = (vb.canonical_title, tuple(_authors(vb.canonical_authors)), vb.isbn_13, vb.source,
               vb.pairing_verdict, vb.pairing_judge, vb.pairing_reason, vb.review_status)
        grouped.setdefault(key, []).append((citation.title, _authors(citation.authors)))
        if holding is not None:
            holdings.append(holding)
            members.append(R.Member(
                holding_id=holding.id, holdings_count=holding.holdings_count or 0,
                status=HoldingStatus(holding.status), course_id=course.course_id,
                course_name=course.course_name, citation_id=citation.id,
                is_required=citation.is_required, enrolled=enrolled,
            ))

    pairings = [
        Pairing(k[0], list(k[1]), k[2], k[3], k[4], k[5], k[6], k[7], originals)
        for k, originals in grouped.items()
    ]

    holding_info = calc = None
    if members:
        rep = next((h for h in holdings if h.id == rec.holding_id),
                   max(holdings, key=lambda h: ((h.holdings_count or 0), -h.id)))
        holding_info = HoldingInfo(
            rep.status, rep.holdings_count or 0, rep.alma_mms_id, rep.matched_edition,
            len({(h.status, h.holdings_count, h.alma_mms_id) for h in holdings}) > 1,
        )
        calc = _calc(members, rec.priority, rec.suggested_copies)

    title = rows[0][1].canonical_title if rows else f"建議 #{rec.id}"
    return Trace(rec.id, title, rec.book_key, rec.rationale, list(syllabi.values()), sources,
                 pairings, holding_info, calc)


# ---------------------------------------------------------------- 渲染

_KIND = {"required": "指定", "optional": "參考", "ai": "AI推薦"}
_STATUS = {"owned_physical": "已有實體", "owned_ebook": "已有電子書", "missing": "館藏不存在",
           "partial": "有書目記錄但無實體館藏", "unknown": "未知"}
_VERDICT = {"same": "同一本", "mismatch": "配錯", None: "尚未稽核"}
_JUDGE = {"rule": "字串規則", "llm": "LLM"}
_REVIEW = {"pending": "待審", "approved": "已核准", "rejected": "已退回"}


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _names(authors: list) -> str:
    return _e("、".join(authors)) if authors else '<span class="tr-dim">（無作者）</span>'


def _sec1(t: Trace) -> str:
    parts = []
    for sy in t.syllabi:
        mentions = list(dict.fromkeys(s.mention for s in t.sources if s.course_id == sy.course_id and s.mention))
        quote = "".join(f'<blockquote class="tr-mention">{_e(m)}</blockquote>' for m in mentions)
        size = (f"共 {sy.length} 字，只顯示前 {len(sy.text)} 字" if sy.length > len(sy.text)
                else f"{sy.length} 字")
        parts.append(
            f'<div class="tr-course"><b>{_e(sy.course_name)}</b> '
            f'<span class="tr-dim">{_e(sy.semester)}</span>{quote}'
            f'<details><summary>完整課綱（{size}）</summary><pre class="tr-pre">{_e(sy.text)}</pre></details></div>'
        )
    return "".join(parts) or '<p class="tr-dim">沒有課綱資料。</p>'


def _sec2(t: Trace) -> str:
    rows = "".join(
        f"<tr><td>{_e(s.course_name)}</td><td>{_e(s.title)}</td><td>{_names(s.authors)}</td>"
        f"<td>{_e(s.edition)}</td><td>{_e(s.isbn)}</td><td>{_e(s.publisher)}</td><td>{_e(s.year)}</td>"
        f"<td>{_e(_KIND[s.kind])}</td><td>{s.confidence:.2f}</td><td>{s.enrolled}</td></tr>"
        for s in t.sources
    )
    return ("<table class=\"tr-tbl\"><thead><tr><th>課程</th><th>書名</th><th>作者</th><th>版次</th><th>ISBN</th>"
            "<th>出版社</th><th>年份</th><th>類別</th><th>信心</th><th>人數</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def _sec3(t: Trace) -> str:
    blocks = []
    for p in t.pairings:
        originals = "".join(f"<div>{_e(title)} <span class=\"tr-dim\">／</span> {_names(authors)}</div>"
                            for title, authors in p.originals)
        judge = f"（{_JUDGE[p.judge]}）" if p.judge in _JUDGE else ""
        reason = f"：{_e(p.reason)}" if p.reason else ""
        review = f' <span class="tr-tag">{_e(_REVIEW.get(p.review_status, p.review_status))}</span>' if p.review_status else ""
        cls = "tr-pair tr-bad" if p.verdict == "mismatch" else "tr-pair"
        blocks.append(
            f'<div class="{cls}">'
            f'<div class="tr-side"><h5>原書（解析結果）{len(p.originals)} 個書目</h5>{originals}</div>'
            f'<div class="tr-mid">⇄</div>'
            f'<div class="tr-side"><h5>配到的書（{_e(p.source)}）</h5>{_e(p.matched_title)} '
            f'<span class="tr-dim">／</span> {_names(p.matched_authors)}'
            f'<div class="tr-dim">ISBN {_e(p.isbn)}</div></div>'
            f'<div class="tr-verdict">稽核判定：<b>{_e(_VERDICT.get(p.verdict, p.verdict))}</b>{judge}{reason}{review}</div>'
            f'</div>'
        )
    return "".join(blocks) or '<p class="tr-dim">沒有配對資料。</p>'


def _sec4(t: Trace) -> str:
    h = t.holding
    if h is None:
        return '<p class="tr-dim">尚無館藏資料。</p>'
    note = ('<div class="tr-dim">注意：各書目的館藏查詢結果不一致，這裡以冊數最大的一筆為準。</div>'
            if h.members_differ else "")
    return (f"<div>狀態：<b>{_e(_STATUS.get(h.status, h.status))}</b>；現有實體 <b>{h.copies}</b> 冊；"
            f"Alma 記錄編號 {_e(h.mms_id)}；比對到的版次 {_e(h.edition)}</div>{note}")


def _sec5(t: Trace) -> str:
    c = t.calc
    if c is None:
        return '<p class="tr-dim">尚無館藏資料，無法重算冊數。</p>'
    people = " ＋ ".join(f"{_e(name)} {n}" for name, n in c.courses)
    lines = [
        f"<li>各課人數（同一門課只算一次）：{people} ＝ <b>{c.enrolled_total}</b> 人"
        + ("（人數不明時視為需要 1 本）" if c.enrolled_total == 0 else "") + "</li>",
        f"<li>每 {c.per_copy} 人 1 本（進位）→ 需求 <b>{c.need}</b> 本</li>",
        f"<li>現有實體 <b>{c.current}</b> 冊（整本書只扣一次）→ 缺口 <b>{c.gap}</b> 本</li>",
    ]
    if c.status == "owned_ebook":
        lines.append("<li>已有電子書 → 不需採購</li>")
    else:
        lines.append(f"<li>單本上限 {c.cap} 本 → 建議 <b>{c.copies}</b> 本</li>")
    lines.append(f"<li>優先級：<b>{_e(c.priority)}</b>（{'有' if c.is_required else '沒有'}任一課指定；"
                 f"{_e(_STATUS.get(c.status, c.status))}）</li>")
    drift = ""
    if not c.consistent:
        drift = (f'<div class="tr-drift">以目前的設定與資料重算是 {_e(c.priority)} ／ {c.copies} 本，'
                 f'但資料庫存的建議是 {_e(c.stored_priority)} ／ {c.stored_copies} 本。'
                 f'可能是上限、館藏或人數在建議產生後改過；重新執行 recommender 會更新。</div>')
    return f"<ol class=\"tr-calc\">{''.join(lines)}</ol>{drift}"


def render_trace(t: Trace) -> str:
    rationale = f'<div class="tr-rationale">系統理由：{_e(t.rationale)}</div>' if t.rationale else ""
    sections = [
        ("1", "① 課綱原文", _sec1(t)), ("2", "② 解析結果", _sec2(t)), ("3", "③ 驗證配對", _sec3(t)),
        ("4", "④ Alma 館藏", _sec4(t)), ("5", "⑤ 冊數怎麼算", _sec5(t)),
    ]
    body = "".join(f'<section class="tr-sec" data-sec="{n}"><h4>{label}</h4>{content}</section>'
                   for n, label, content in sections)
    return f'<div class="trace"><h3 class="tr-title">{_e(t.title)}</h3>{rationale}{body}</div>'
