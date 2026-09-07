"""採購決策儀表板（唯讀）。

FastAPI 服務，讀既有 DB 呈現缺口分析：KPI 概覽、採購優先級與館藏狀態分布、
可篩選的採購建議明細、待人工審核清單。啟動：`library-agent dashboard`。

圖表用內嵌 HTML/CSS bar（無外部 CDN/JS 依賴、離線可用），配色取自 dataviz
技能的 status palette，並以文字標籤 + 數值確保辨識不依賴顏色。
"""
import csv
import html
import io
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select

from library_agent.agents.crawler import DATA_DIR
from library_agent.db.models import (
    Citation,
    Course,
    HoldingCheck,
    Recommendation,
    VerifiedBook,
)
from library_agent.db.session import SessionLocal

app = FastAPI(title="Library Agent 採購決策儀表板")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

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
_STATUS_RANK = {k: i for i, (k, _, _) in enumerate(_STATUS_META)}


def _not_rejected():
    return or_(VerifiedBook.review_status.is_(None), VerifiedBook.review_status != "rejected")


def _load_data() -> dict:
    with SessionLocal() as s:
        kpis = {
            "courses": s.scalar(select(func.count()).select_from(Course)) or 0,
            "citations": s.scalar(select(func.count()).select_from(Citation)) or 0,
            "verified": s.scalar(
                select(func.count()).select_from(VerifiedBook).where(VerifiedBook.verified.is_(True))
            ) or 0,
            "pending": s.scalar(
                select(func.count()).select_from(VerifiedBook).where(VerifiedBook.review_status == "pending")
            ) or 0,
        }

        prio = {k: 0 for k, _, _ in _PRIORITY_META}
        copies = 0
        for priority, cnt, csum in s.execute(
            select(Recommendation.priority, func.count(), func.sum(Recommendation.suggested_copies))
            .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
            .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
            .where(_not_rejected())
            .group_by(Recommendation.priority)
        ).all():
            prio[priority] = cnt
            if priority != "skip":
                copies += int(csum or 0)
        kpis["copies"] = copies

        status = {k: 0 for k, _, _ in _STATUS_META}
        for st, cnt in s.execute(
            select(HoldingCheck.status, func.count()).group_by(HoldingCheck.status)
        ).all():
            status[st] = status.get(st, 0) + cnt

        recs = s.execute(
            select(
                Recommendation.priority, Course.course_name, VerifiedBook.canonical_title,
                Citation.is_required, HoldingCheck.status, Recommendation.suggested_copies,
                Recommendation.rationale, Citation.raw_mention,
            )
            .join(Course, Course.course_id == Recommendation.course_id)
            .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
            .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
            .join(Citation, Citation.id == VerifiedBook.citation_id)
            .where(_not_rejected())
        ).all()
        recs = sorted(recs, key=lambda r: (_PRIORITY_RANK.get(r[0], 9), r[1] or ""))

        holdings = s.execute(
            select(
                HoldingCheck.status, Course.course_name, VerifiedBook.canonical_title,
                HoldingCheck.holdings_count, HoldingCheck.alma_mms_id,
            )
            .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
            .join(Citation, Citation.id == VerifiedBook.citation_id)
            .join(Course, Course.course_id == Citation.course_id)
        ).all()
        holdings = sorted(holdings, key=lambda r: (_STATUS_RANK.get(r[0], 9), r[1] or ""))

        pending = s.execute(
            select(Course.course_name, Citation.title, Citation.confidence, VerifiedBook.source,
                   Citation.is_required, Citation.raw_mention)
            .join(Citation, Citation.id == VerifiedBook.citation_id)
            .join(Course, Course.course_id == Citation.course_id)
            .where(VerifiedBook.review_status == "pending")
            .order_by(Course.course_name)
        ).all()

    return {"kpis": kpis, "priority": prio, "status": status, "recs": recs, "pending": pending, "holdings": holdings}


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
    tiles = [
        ("課程數", k["courses"], ""),
        ("書目數", k["citations"], ""),
        ("已驗證", k["verified"], ""),
        ("待人工審核", k["pending"], "warn" if k["pending"] else ""),
        ("建議採購總冊數", k["copies"], "accent"),
    ]
    return "".join(
        f'<div class="kpi"><div class="val {cls}">{val}</div><div class="lbl">{lbl}</div></div>'
        for lbl, val, cls in tiles
    )


def _rec_row(priority: str, course: str, title: str, btype: str, status_html: str,
             copies_html: str, rationale: str) -> str:
    pcolor = _PRIORITY_COLOR.get(priority, _PENDING_COLOR)
    plabel = _PRIORITY_LABEL.get(priority, _PENDING_LABEL)
    return (
        f'<tr data-priority="{html.escape(priority)}">'
        f'<td><span class="pill" style="background:{pcolor}">{html.escape(plabel)}</span></td>'
        f'<td>{html.escape(course or "")}</td>'
        f'<td>{html.escape(title or "")}</td>'
        f'<td>{btype}</td>'
        f'<td>{status_html}</td>'
        f'<td class="num">{copies_html}</td>'
        f'<td class="rationale">{html.escape(rationale or "")}</td></tr>'
    )


def _is_ai_recommended(raw_mention: str | None) -> bool:
    """discoverer 產生的 AI 推薦書，raw_mention 會標 [AI推薦]（見 discoverer.py）。"""
    return bool(raw_mention) and "AI推薦" in raw_mention


def _book_type_label(is_required: bool, raw_mention: str | None) -> str:
    """類別標籤：AI 推薦 > 指定 > 參考。AI 推薦以橘色標籤與教師指定/參考區隔。"""
    if _is_ai_recommended(raw_mention):
        return f'<span class="tag tag-ai">AI推薦</span>'
    return f'<span class="tag">{"指定" if is_required else "參考"}</span>'


def _rec_table(recs: list, pending: list) -> str:
    if not recs and not pending:
        return '<p class="empty">尚無採購建議，請先執行 pipeline。</p>'
    body = []
    for priority, course, title, is_required, status, copies, rationale, raw_mention in recs:
        scolor = _STATUS_COLOR.get(status, "#898781")
        slabel = _STATUS_LABEL.get(status, status)
        btype = _book_type_label(is_required, raw_mention)
        status_html = f'<span class="pill" style="background:{scolor}">{html.escape(slabel)}</span>'
        body.append(_rec_row(priority, course, title, btype, status_html, str(copies), rationale))
    # 待審核的書還沒進 librarian/recommender，沒有館藏狀態/冊數，理由欄改顯示驗證信心分數與來源
    for course, title, confidence, source, is_required, raw_mention in pending:
        rationale = f"confidence {confidence:.2f} · {source or ''}"
        btype = _book_type_label(is_required, raw_mention)
        body.append(_rec_row("pending", course, title, btype, "—", "—", rationale))
    return (
        '<table><thead><tr><th>優先級</th><th>課程</th><th>書名</th><th>類別</th>'
        '<th>館藏狀態</th><th>冊數</th><th>理由</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _holdings_table(holdings: list) -> str:
    if not holdings:
        return '<p class="empty">尚無館藏資料，請先執行 pipeline。</p>'
    body = []
    for status, course, title, count, mms_id in holdings:
        scolor = _STATUS_COLOR.get(status, "#898781")
        slabel = _STATUS_LABEL.get(status, status)
        body.append(
            f'<tr data-status="{html.escape(status)}">'
            f'<td><span class="pill" style="background:{scolor}">{html.escape(slabel)}</span></td>'
            f'<td>{html.escape(course or "")}</td>'
            f'<td>{html.escape(title or "")}</td>'
            f'<td class="num">{count}</td>'
            f'<td>{html.escape(mms_id or "")}</td></tr>'
        )
    return (
        '<table><thead><tr><th>館藏狀態</th><th>課程</th><th>書名</th>'
        '<th>冊數</th><th>Alma 書目 ID</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _render(data: dict) -> str:
    prio_bars = _bars(
        [(_PRIORITY_LABEL[k], data["priority"].get(k, 0), _PRIORITY_COLOR[k])
         for k, _, _ in _PRIORITY_META]
        + [(_PENDING_LABEL, data["kpis"]["pending"], _PENDING_COLOR)]
    )
    status_bars = _bars([(_STATUS_LABEL[k], data["status"].get(k, 0), _STATUS_COLOR[k])
                        for k, _, _ in _STATUS_META])
    filter_btns = '<button class="fbtn active" data-f="all">全部</button>' + "".join(
        f'<button class="fbtn" data-f="{k}">{html.escape(n)}</button>' for k, n, _ in _PRIORITY_META
    ) + f'<button class="fbtn" data-f="pending">{_PENDING_LABEL}</button>'
    status_filter_btns = '<button class="sfbtn active" data-sf="all">全部</button>' + "".join(
        f'<button class="sfbtn" data-sf="{k}">{html.escape(n)}</button>' for k, n, _ in _STATUS_META
    )
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>採購決策儀表板</title>
<script src="/static/mermaid.min.js"></script>
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
.exportlink {{ font-size:12px; font-weight:400; color:var(--text-secondary); text-decoration:none;
  border:1px solid var(--border); border-radius:6px; padding:2px 9px; margin-left:6px; }}
.exportlink:hover {{ background:var(--track); }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:16px; }}
.runbar {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
.runbtn {{ font:inherit; font-size:14px; font-weight:600; padding:8px 18px; border-radius:8px; cursor:pointer;
  border:1px solid var(--border); background:var(--text-primary); color:var(--surface); }}
.runbtn:disabled {{ opacity:.45; cursor:not-allowed; }}
.limitlbl {{ font-size:13px; color:var(--text-secondary); }}
.limitlbl input {{ font:inherit; width:112px; padding:5px 8px; border:1px solid var(--border); border-radius:6px; margin-left:4px; }}
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
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:16px; }}
.kpi {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }}
.kpi .val {{ font-size:30px; font-weight:700; letter-spacing:-.5px; }}
.kpi .val.accent {{ color:var(--critical); }}
.kpi .val.warn {{ color:var(--warning); }}
.kpi .lbl {{ font-size:12px; color:var(--text-secondary); margin-top:2px; }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:720px) {{ .charts {{ grid-template-columns:1fr; }} }}
.tables {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }}
@media (max-width:960px) {{ .tables {{ grid-template-columns:1fr; }} }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:8px 0; }}
.bar-label {{ width:96px; flex:none; text-align:right; font-size:13px; color:var(--text-secondary); }}
.bar-track {{ flex:1; height:20px; background:var(--track); border-radius:4px; }}
.bar-fill {{ height:100%; border-radius:0 4px 4px 0; min-width:2px; display:block; }}
.bar-value {{ width:44px; flex:none; font-weight:600; font-variant-numeric:tabular-nums; }}
.bar-row:hover .bar-fill {{ filter:brightness(1.08); }}
.filters {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }}
.fbtn, .sfbtn {{ font:inherit; font-size:12.5px; padding:4px 12px; border-radius:99px; cursor:pointer;
  border:1px solid var(--border); background:transparent; color:var(--text-secondary); }}
.fbtn.active, .sfbtn.active {{ background:var(--text-primary); color:var(--surface); border-color:var(--text-primary); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--gridline); padding:8px 10px; text-align:left; vertical-align:top; }}
th {{ color:var(--text-secondary); font-weight:600; white-space:nowrap; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }}
td.rationale {{ color:var(--text-secondary); font-size:12px; max-width:340px; }}
.pill {{ display:inline-block; padding:1px 9px; border-radius:99px; font-size:11.5px; font-weight:600; color:#fff; white-space:nowrap; }}
.tag {{ display:inline-block; padding:1px 7px; border-radius:5px; font-size:11.5px; border:1px solid var(--border); color:var(--text-secondary); white-space:nowrap; }}
.tag-ai {{ border-color:#e0900a; color:#fff; background:#e0900a; }}
.empty {{ color:var(--muted); font-size:13px; }}
</style></head><body><div class="wrap">
<h1>圖書館採購決策儀表板</h1>
<div class="sub">缺口分析 · 資料來自當下資料庫</div>

<div class="card runbar">
  <button id="runbtn" class="runbtn">▶ 執行 pipeline</button>
  <label class="limitlbl">限制筆數 <input id="limit" type="number" min="1" placeholder="留空 = 全跑"></label>
  <label class="uploadbtn">上傳 xlsx<input id="fileupload" type="file" accept=".xlsx" multiple hidden></label>
  <div class="ffwrap">
    <button id="ffbtn" class="uploadbtn" type="button">篩選 (全部)</button>
    <div id="filefilter" class="filefilter" hidden></div>
  </div>
  <span id="runstatus" class="runstatus">—</span>
  <span id="counts" class="counts"></span>
</div>

<div class="card">
  <h2>Pipeline 進度</h2>
  <pre class="mermaid" id="flowchart">
flowchart LR
    crawler[crawler]
    parser[parser]
    discoverer[discoverer]
    validator[validator]
    librarian[librarian]
    human_review[human_review]
    recommender[recommender]
    crawler --> parser --> discoverer --> validator
    validator --> librarian --> recommender
    validator --> human_review
  </pre>
</div>

<div class="kpi-grid">{_kpi_tiles(data["kpis"])}</div>

<div class="charts">
  <div class="card"><h2>採購優先級分布</h2>{prio_bars}</div>
  <div class="card"><h2>館藏狀態分布</h2>{status_bars}</div>
</div>

<div class="tables">
  <div class="card">
    <h2>採購建議明細
      <a class="exportlink" href="/export/recommendations">匯出 CSV</a>
      <a class="exportlink" href="/export/pending">匯出待審核 CSV</a>
    </h2>
    <div class="filters">{filter_btns}</div>
    {_rec_table(data["recs"], data["pending"])}
  </div>

  <div class="card">
    <h2>館藏明細</h2>
    <div class="filters">{status_filter_btns}</div>
    {_holdings_table(data["holdings"])}
  </div>
</div>
</div>
<script>
mermaid.initialize({{ startOnLoad: true, theme: 'neutral', securityLevel: 'loose' }});

const _NODE_COLOR = {{ pending: '#898781', running: '#fab219', done: '#0ca30c', error: '#d03b3b' }};

function _updateFlowchart(nodes) {{
  const svg = document.querySelector('#flowchart svg');
  if (!svg) return;  // Mermaid 尚未完成首次渲染
  nodes.forEach(function(n) {{
    const g = svg.querySelector(`[id*="flowchart-${{n.name}}-"]`);
    if (!g) return;
    const shape = g.querySelector('rect, polygon, .node-bkg') || g.querySelector('*');
    if (shape) shape.style.fill = _NODE_COLOR[n.status] || _NODE_COLOR.pending;
  }});
}}

document.querySelectorAll('.fbtn').forEach(function(b){{
  b.addEventListener('click', function(){{
    document.querySelectorAll('.fbtn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    var f=b.dataset.f;
    b.closest('.card').querySelectorAll('tbody tr').forEach(function(tr){{
      tr.style.display = (f==='all' || tr.dataset.priority===f) ? '' : 'none';
    }});
  }});
}});

document.querySelectorAll('.sfbtn').forEach(function(b){{
  b.addEventListener('click', function(){{
    document.querySelectorAll('.sfbtn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    var f=b.dataset.sf;
    b.closest('.card').querySelectorAll('tbody tr').forEach(function(tr){{
      tr.style.display = (f==='all' || tr.dataset.status===f) ? '' : 'none';
    }});
  }});
}});

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
    if (d.nodes) _updateFlowchart(d.nodes);
    if (d.status === 'running') {{
      _sawRunning = true;
      setTimeout(_poll, 3000);
    }} else if (_sawRunning && (d.status === 'done' || d.status === 'error')) {{
      // 親眼看過它從 running 跑到這裡結束 → 整頁資料（KPI/圖表/建議明細）都舊了，重整一次
      location.reload();
    }}
  }} catch (e) {{ _el('runstatus').textContent = '無法連線'; }}
}}
// 更新「篩選」按鈕上的選取計數
function _updateFfLabel() {{
  const chks = Array.from(document.querySelectorAll('.ffchk'));
  const picked = chks.filter(c => c.checked).length;
  const txt = (chks.length === 0 || picked === chks.length) ? '全部' : `${{picked}}/${{chks.length}}`;
  _el('ffbtn').textContent = '篩選 (' + txt + ')';
}}

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

_el('runbtn').onclick = async () => {{
  const lim = _el('limit').value.trim();
  // 收集勾選的檔案；全勾（或沒有勾選框）＝不限定，送 null
  const chks = Array.from(document.querySelectorAll('.ffchk'));
  const picked = chks.filter(c => c.checked).map(c => c.value);
  const allChecked = chks.length > 0 && picked.length === chks.length;
  const source_files = (chks.length === 0 || allChecked) ? null : picked;
  if (source_files && source_files.length === 0) {{ alert('請至少勾選一個 xlsx，或全部勾選代表全跑'); return; }}
  if (!lim && !confirm('未填限制筆數＝全跑選中檔案裡剩下所有課程，可能需要數小時。確定要執行嗎？')) return;
  const q = lim ? ('?limit=' + encodeURIComponent(lim)) : '';
  _el('runbtn').disabled = true;
  await fetch('/run' + q, {{
    method:'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(source_files),
  }});
  _poll();
}};
_loadFiles();
_poll();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _render(_load_data())


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
    header = ["優先級", "課程", "書名", "類別", "館藏狀態", "冊數", "理由"]
    rows = [
        (
            _PRIORITY_LABEL.get(priority, priority),
            course,
            title,
            "AI推薦" if _is_ai_recommended(raw_mention) else ("指定" if is_required else "參考"),
            _STATUS_LABEL.get(status, status),
            copies,
            rationale,
        )
        for priority, course, title, is_required, status, copies, rationale, raw_mention in data["recs"]
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
        for course, title, confidence, source, is_required, raw_mention in data["pending"]
    ]
    return _csv_response(header, rows, "pending_review")


# ---------- 執行 pipeline + 進度 ----------

_run_lock = threading.Lock()
_run_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "error": None, "run_id": None}


def _run_pipeline(run_id: str, limit: int | None, source_files: list[str] | None) -> None:
    from library_agent.graph import build_graph
    initial: dict = {}
    if limit:
        initial["limit"] = limit
    if source_files:
        initial["source_files"] = source_files
    try:
        build_graph(run_id).invoke(initial)
        status, error = "done", None
    except Exception as e:  # 背景執行緒的例外要自己接，否則靜默消失
        status, error = "error", str(e)[:500]
    with _run_lock:
        _run_state.update(status=status, finished_at=time.time(), error=error)


@app.post("/run")
def start_run(limit: int | None = None, source_files: list[str] | None = Body(default=None)) -> dict:
    """在背景執行緒啟動整條 pipeline。limit=None 全跑；source_files=None/空＝讀全部 xlsx；一次只准一個。"""
    from library_agent.graph import _init_run

    with _run_lock:
        if _run_state["status"] == "running":
            return {"ok": False, "message": "pipeline 已在執行中"}
        run_id = str(time.time())
        _init_run(run_id)
        _run_state.update(status="running", started_at=time.time(), finished_at=None, error=None, run_id=run_id)
    threading.Thread(target=_run_pipeline, args=(run_id, limit, source_files), daemon=True).start()
    return {"ok": True}


# ---------- xlsx 檔案管理 ----------

@app.get("/files")
def list_files() -> dict:
    """列出 data/ 目錄現有的 xlsx 檔名，供前端篩選 UI 用。"""
    names = sorted(p.name for p in DATA_DIR.glob("*.xlsx"))
    return {"files": names}


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
