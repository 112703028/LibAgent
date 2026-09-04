"""採購決策儀表板（唯讀）。

FastAPI 服務，讀既有 DB 呈現缺口分析：KPI 概覽、採購優先級與館藏狀態分布、
可篩選的採購建議明細、待人工審核清單。啟動：`library-agent dashboard`。

圖表用內嵌 HTML/CSS bar（無外部 CDN/JS 依賴、離線可用），配色取自 dataviz
技能的 status palette，並以文字標籤 + 數值確保辨識不依賴顏色。
"""
import html
import threading
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select

from library_agent.db.models import (
    Citation,
    Course,
    HoldingCheck,
    Recommendation,
    VerifiedBook,
)
from library_agent.db.session import SessionLocal

app = FastAPI(title="Library Agent 採購決策儀表板")

# (key, 顯示名稱, 顏色)  顏色取自 dataviz status palette
_PRIORITY_META = [
    ("high", "HIGH 高", "#d03b3b"),
    ("medium", "MEDIUM 中", "#fab219"),
    ("low", "LOW 低", "#0ca30c"),
    ("skip", "SKIP 略過", "#898781"),
]
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
                Recommendation.rationale,
            )
            .join(Course, Course.course_id == Recommendation.course_id)
            .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
            .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
            .join(Citation, Citation.id == VerifiedBook.citation_id)
            .where(_not_rejected())
        ).all()
        recs = sorted(recs, key=lambda r: (_PRIORITY_RANK.get(r[0], 9), r[1] or ""))

        pending = s.execute(
            select(Course.course_name, Citation.title, Citation.confidence, VerifiedBook.source)
            .join(Citation, Citation.id == VerifiedBook.citation_id)
            .join(Course, Course.course_id == Citation.course_id)
            .where(VerifiedBook.review_status == "pending")
            .order_by(Course.course_name)
        ).all()

    return {"kpis": kpis, "priority": prio, "status": status, "recs": recs, "pending": pending}


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


def _rec_table(recs: list) -> str:
    if not recs:
        return '<p class="empty">尚無採購建議，請先執行 pipeline。</p>'
    body = []
    for priority, course, title, is_required, status, copies, rationale in recs:
        pcolor = _PRIORITY_COLOR.get(priority, "#898781")
        plabel = _PRIORITY_LABEL.get(priority, priority)
        scolor = _STATUS_COLOR.get(status, "#898781")
        slabel = _STATUS_LABEL.get(status, status)
        btype = "指定" if is_required else "參考"
        body.append(
            f'<tr data-priority="{html.escape(priority)}">'
            f'<td><span class="pill" style="background:{pcolor}">{html.escape(plabel)}</span></td>'
            f'<td>{html.escape(course or "")}</td>'
            f'<td>{html.escape(title or "")}</td>'
            f'<td><span class="tag">{btype}</span></td>'
            f'<td><span class="pill" style="background:{scolor}">{html.escape(slabel)}</span></td>'
            f'<td class="num">{copies}</td>'
            f'<td class="rationale">{html.escape(rationale or "")}</td></tr>'
        )
    return (
        '<table><thead><tr><th>優先級</th><th>課程</th><th>書名</th><th>類別</th>'
        '<th>館藏狀態</th><th>冊數</th><th>理由</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def _pending_list(pending: list) -> str:
    if not pending:
        return '<p class="empty">目前沒有待審書目。</p>'
    items = []
    for course, title, confidence, source in pending:
        items.append(
            f'<li><span class="pconf">conf {confidence:.2f}</span> '
            f'<b>{html.escape(title or "")}</b> '
            f'<span class="pmeta">{html.escape(course or "")} · {html.escape(source or "")}</span></li>'
        )
    return f'<ul class="pending">{"".join(items)}</ul>'


def _render(data: dict) -> str:
    prio_bars = _bars([(_PRIORITY_LABEL[k], data["priority"].get(k, 0), _PRIORITY_COLOR[k])
                       for k, _, _ in _PRIORITY_META])
    status_bars = _bars([(_STATUS_LABEL[k], data["status"].get(k, 0), _STATUS_COLOR[k])
                        for k, _, _ in _STATUS_META])
    filter_btns = '<button class="fbtn active" data-f="all">全部</button>' + "".join(
        f'<button class="fbtn" data-f="{k}">{html.escape(n)}</button>' for k, n, _ in _PRIORITY_META
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
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:16px; }}
.kpi {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 16px; }}
.kpi .val {{ font-size:30px; font-weight:700; letter-spacing:-.5px; }}
.kpi .val.accent {{ color:var(--critical); }}
.kpi .val.warn {{ color:var(--warning); }}
.kpi .lbl {{ font-size:12px; color:var(--text-secondary); margin-top:2px; }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:720px) {{ .charts {{ grid-template-columns:1fr; }} }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:8px 0; }}
.bar-label {{ width:96px; flex:none; text-align:right; font-size:13px; color:var(--text-secondary); }}
.bar-track {{ flex:1; height:20px; background:var(--track); border-radius:4px; }}
.bar-fill {{ height:100%; border-radius:0 4px 4px 0; min-width:2px; display:block; }}
.bar-value {{ width:44px; flex:none; font-weight:600; font-variant-numeric:tabular-nums; }}
.bar-row:hover .bar-fill {{ filter:brightness(1.08); }}
.filters {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }}
.fbtn {{ font:inherit; font-size:12.5px; padding:4px 12px; border-radius:99px; cursor:pointer;
  border:1px solid var(--border); background:transparent; color:var(--text-secondary); }}
.fbtn.active {{ background:var(--text-primary); color:var(--surface); border-color:var(--text-primary); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--gridline); padding:8px 10px; text-align:left; vertical-align:top; }}
th {{ color:var(--text-secondary); font-weight:600; white-space:nowrap; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }}
td.rationale {{ color:var(--text-secondary); font-size:12px; max-width:340px; }}
.pill {{ display:inline-block; padding:1px 9px; border-radius:99px; font-size:11.5px; font-weight:600; color:#fff; white-space:nowrap; }}
.tag {{ display:inline-block; padding:1px 7px; border-radius:5px; font-size:11.5px; border:1px solid var(--border); color:var(--text-secondary); white-space:nowrap; }}
.empty {{ color:var(--muted); font-size:13px; }}
ul.pending {{ list-style:none; margin:0; padding:0; }}
ul.pending li {{ padding:6px 0; border-bottom:1px solid var(--gridline); font-size:13px; }}
.pconf {{ display:inline-block; min-width:64px; color:var(--warning); font-weight:600; font-variant-numeric:tabular-nums; }}
.pmeta {{ color:var(--muted); font-size:12px; }}
</style></head><body><div class="wrap">
<h1>圖書館採購決策儀表板</h1>
<div class="sub">缺口分析 · 資料來自當下資料庫</div>

<div class="card runbar">
  <button id="runbtn" class="runbtn">▶ 執行 pipeline</button>
  <label class="limitlbl">限制筆數 <input id="limit" type="number" min="1" placeholder="留空 = 全跑"></label>
  <span id="runstatus" class="runstatus">—</span>
  <span id="counts" class="counts"></span>
</div>

<div class="kpi-grid">{_kpi_tiles(data["kpis"])}</div>

<div class="charts">
  <div class="card"><h2>採購優先級分布</h2>{prio_bars}</div>
  <div class="card"><h2>館藏狀態分布</h2>{status_bars}</div>
</div>

<div class="card">
  <h2>採購建議明細</h2>
  <div class="filters">{filter_btns}</div>
  {_rec_table(data["recs"])}
</div>

<div class="card">
  <h2>待人工審核（pending）</h2>
  {_pending_list(data["pending"])}
</div>
</div>
<script>
document.querySelectorAll('.fbtn').forEach(function(b){{
  b.addEventListener('click', function(){{
    document.querySelectorAll('.fbtn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    var f=b.dataset.f;
    document.querySelectorAll('tbody tr').forEach(function(tr){{
      tr.style.display = (f==='all' || tr.dataset.priority===f) ? '' : 'none';
    }});
  }});
}});

const _el = id => document.getElementById(id);
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
    if (d.status === 'running') setTimeout(_poll, 3000);
  }} catch (e) {{ _el('runstatus').textContent = '無法連線'; }}
}}
_el('runbtn').onclick = async () => {{
  const lim = _el('limit').value.trim();
  if (!lim && !confirm('未填限制筆數＝全跑剩下所有課程，可能需要數小時。確定要執行嗎？')) return;
  const q = lim ? ('?limit=' + encodeURIComponent(lim)) : '';
  _el('runbtn').disabled = true;
  await fetch('/run' + q, {{method:'POST'}});
  _poll();
}};
_poll();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _render(_load_data())


# ---------- 執行 pipeline + 進度 ----------

_run_lock = threading.Lock()
_run_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "error": None, "run_id": None}


def _run_pipeline(run_id: str, limit: int | None) -> None:
    from library_agent.graph import build_graph
    try:
        build_graph(run_id).invoke({"limit": limit} if limit else {})
        status, error = "done", None
    except Exception as e:  # 背景執行緒的例外要自己接，否則靜默消失
        status, error = "error", str(e)[:500]
    with _run_lock:
        _run_state.update(status=status, finished_at=time.time(), error=error)


@app.post("/run")
def start_run(limit: int | None = None) -> dict:
    """在背景執行緒啟動整條 pipeline。limit=None 全跑；一次只准一個。"""
    from library_agent.graph import _init_run

    with _run_lock:
        if _run_state["status"] == "running":
            return {"ok": False, "message": "pipeline 已在執行中"}
        run_id = str(time.time())
        _init_run(run_id)
        _run_state.update(status="running", started_at=time.time(), finished_at=None, error=None, run_id=run_id)
    threading.Thread(target=_run_pipeline, args=(run_id, limit), daemon=True).start()
    return {"ok": True}


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
            "verified": s.scalar(select(func.count()).select_from(VerifiedBook)) or 0,
            "holdings": s.scalar(select(func.count()).select_from(HoldingCheck)) or 0,
            "recommendations": s.scalar(select(func.count()).select_from(Recommendation)) or 0,
        }
        node_rows = {}
        if st["run_id"]:
            rows = s.execute(
                select(PipelineNodeRun.node_name, PipelineNodeRun.status,
                       PipelineNodeRun.started_at, PipelineNodeRun.finished_at)
                .where(PipelineNodeRun.run_id == st["run_id"])
                .order_by(PipelineNodeRun.id.desc())
            ).all()
            for node_name, node_status, started_at, finished_at in rows:
                if node_name not in node_rows:  # 每個節點只取最新一筆（id desc 已排序）
                    node_rows[node_name] = (node_status, started_at, finished_at)

    nodes = []
    now = time.time()
    for name in NODE_NAMES:
        node_status, started_at, finished_at = node_rows.get(name, ("pending", None, None))
        elapsed = None
        if started_at:
            end = finished_at.timestamp() if finished_at else now
            elapsed = round(end - started_at.timestamp())
        nodes.append({"name": name, "status": node_status, "elapsed": elapsed})

    elapsed = None
    if st["started_at"]:
        elapsed = round((st["finished_at"] or time.time()) - st["started_at"])
    return {"status": st["status"], "elapsed": elapsed, "error": st["error"], "counts": counts, "nodes": nodes}
