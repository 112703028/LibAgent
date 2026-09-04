# Pipeline 即時進度流程圖 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dashboard 顯示 LangGraph 風格的流程圖，即時上色標示 pipeline 目前跑到哪個節點。

**Architecture:** 新增 DB 表 `pipeline_node_runs` 記錄每個節點的執行狀態；`graph.py` 的 `build_graph()` 對每個節點包一層 tracking wrapper，執行前後寫入該表；`dashboard.py` 的 `/status` 端點多回傳節點狀態陣列；前端用 vendor 進來的 Mermaid.js 畫 flowchart，依輪詢到的狀態動態上色。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、LangGraph、Mermaid.js（vendored，no CDN）、純 JS（no build step）

**Spec:** [docs/superpowers/specs/2026-09-04-pipeline-progress-flowchart-design.md](../specs/2026-09-04-pipeline-progress-flowchart-design.md)

## Global Constraints

- 不使用 WebSocket；沿用現有前端輪詢架構（3 秒間隔）。
- Mermaid.js 必須 vendor 到本地 `src/library_agent/static/`，不可用 CDN。
- 節點狀態粒度為「整個節點」（pending/running/done/error），不做節點內部單筆進度。
- 節點例外時必須重新拋出（`raise`），不可吞掉——現有 `dashboard.py` 的 `_run_pipeline` 例外處理行為不可改變。
- `pipeline_node_runs` 寫入用短生命週期的 `SessionLocal()`，每次寫完即 commit，不長時間持有連線。
- 遵循現有測試風格：純函式單元測試，不碰真實 DB/網路（見 `tests/test_validator.py`、`tests/conftest.py`）。

---

## File Structure

- **Modify** `src/library_agent/db/models.py` — 新增 `PipelineNodeRun` model。
- **Create** `alembic/versions/<hash>_add_pipeline_node_runs.py` — migration。
- **Modify** `src/library_agent/graph.py` — 新增 `_mark_status()` helper + `_with_tracking()` wrapper + `build_graph(run_id=None)` 簽名變更。
- **Modify** `src/library_agent/dashboard.py` — `start_run()` 產生 run_id 並初始化 7 筆 pending；`/status` 回傳 `nodes` 陣列；掛載 `/static`；前端 HTML 內嵌 Mermaid flowchart + 上色 JS。
- **Create** `src/library_agent/static/mermaid.min.js` — vendored 檔案（下載動作，非程式碼撰寫）。
- **Create** `tests/test_graph_tracking.py` — 測試 `_mark_status()` / `_with_tracking()` 純邏輯（用假 session，不碰真實 DB，比照 `test_validator.py` 的 `_FakeSession` 模式）。

---

## Task 1: DB model + migration

**Files:**
- Modify: `src/library_agent/db/models.py`
- Create: `alembic/versions/<hash>_add_pipeline_node_runs.py`

**Interfaces:**
- Produces: `PipelineNodeRun` model with columns `id, run_id, node_name, status, started_at, finished_at, error`. Later tasks import this as `from library_agent.db.models import PipelineNodeRun`.

- [ ] **Step 1: 在 `models.py` 新增 `PipelineNodeRun`**

在 [src/library_agent/db/models.py](../../../src/library_agent/db/models.py) 檔案最後（`Recommendation` class 之後）新增：

```python
# Pipeline 執行時每個節點的即時狀態（供 dashboard 流程圖顯示進度）
class PipelineNodeRun(Base):
    __tablename__ = "pipeline_node_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(50), nullable=False)
    node_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pending | running | done | error
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 2: 產生 migration**

Run: `alembic revision --autogenerate -m "add pipeline_node_runs"`

檢查產生的檔案內容是否正確對應 Step 1 的欄位（`upgrade()` 應該是 `op.create_table('pipeline_node_runs', ...)`，`downgrade()` 是 `op.drop_table('pipeline_node_runs')`）。若 autogenerate 因環境問題失敗，改為比照 [alembic/versions/b2f4a1c9d3e7_add_review_status.py](../../../alembic/versions/b2f4a1c9d3e7_add_review_status.py) 的風格手寫：

```python
"""add pipeline_node_runs

Revision ID: <自動產生>
Revises: b2f4a1c9d3e7
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '<自動產生>'
down_revision: Union[str, Sequence[str], None] = 'b2f4a1c9d3e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pipeline_node_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=50), nullable=False),
        sa.Column('node_name', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('pipeline_node_runs')
```

- [ ] **Step 3: 確認 docker-compose 的 postgres 正在跑，執行 migration**

Run: `docker ps` (確認 `library_agent-postgres-1` 是 Up 狀態；沒有的話先 `docker-compose up -d`)
Run: `alembic upgrade head`
Expected: 輸出顯示套用到新的 revision，無錯誤。

- [ ] **Step 4: 確認資料表建立成功**

Run: `docker exec library_agent-postgres-1 psql -U postgres -d library_agent -c "\d pipeline_node_runs"`
Expected: 顯示 7 個欄位（id, run_id, node_name, status, started_at, finished_at, error）。

- [ ] **Step 5: Commit**

```bash
git add src/library_agent/db/models.py alembic/versions/
git commit -m "新增 pipeline_node_runs 表，記錄節點執行狀態"
```

---

## Task 2: graph.py tracking wrapper

**Files:**
- Modify: `src/library_agent/graph.py`
- Test: `tests/test_graph_tracking.py`

**Interfaces:**
- Consumes: `PipelineNodeRun` model from Task 1 (`from library_agent.db.models import PipelineNodeRun`), `SessionLocal` from `library_agent.db.session`.
- Produces:
  - `NODE_NAMES: list[str]` — 固定順序的 7 個節點名稱（`["crawler", "parser", "discoverer", "validator", "librarian", "human_review", "recommender"]`），Task 3 的 `/status` 端點會 import 這個常數來決定回傳順序。
  - `_mark_status(run_id: str, node_name: str, status: str, *, started_at=None, finished_at=None, error=None) -> None`
  - `_init_run(run_id: str) -> None` — 為 `NODE_NAMES` 全部寫一筆 `status="pending"`。Task 3 的 `start_run()` 會呼叫這個。
  - `build_graph(run_id: str | None = None) -> CompiledGraph` — 簽名變更，`run_id=None` 時行為與現在完全相同（不寫 DB）。
  - 模組層級 `pipeline = build_graph()`（不變，供既有 `if __name__ == "__main__"` 情境使用，`run_id=None`）。

- [ ] **Step 1: 寫失敗測試 — `_mark_status` 寫入正確欄位**

Create `tests/test_graph_tracking.py`:

```python
from datetime import datetime, timezone

from library_agent.graph import NODE_NAMES, _init_run, _mark_status


class _FakeQuery:
    def __init__(self, session):
        self._session = session

    def where(self, *a, **k):
        return self


class _FakeSession:
    """比照 tests/test_validator.py 的 _FakeSession 模式：記錄呼叫順序與寫入內容。"""

    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_node_names_order_matches_graph_topology():
    assert NODE_NAMES == [
        "crawler", "parser", "discoverer", "validator",
        "librarian", "human_review", "recommender",
    ]


def test_mark_status_running_sets_started_at(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    now = datetime.now(timezone.utc)
    _mark_status("run1", "crawler", "running", started_at=now)

    assert fake.committed is True
    assert len(fake.added) == 1
    row = fake.added[0]
    assert row.run_id == "run1"
    assert row.node_name == "crawler"
    assert row.status == "running"
    assert row.started_at == now


def test_mark_status_error_sets_error_message(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    _mark_status("run1", "validator", "error", error="boom")

    row = fake.added[0]
    assert row.status == "error"
    assert row.error == "boom"


def test_init_run_writes_pending_for_all_nodes(monkeypatch):
    fake = _FakeSession()
    monkeypatch.setattr("library_agent.graph.SessionLocal", lambda: fake)

    _init_run("run1")

    assert len(fake.added) == len(NODE_NAMES)
    assert all(row.status == "pending" for row in fake.added)
    assert [row.node_name for row in fake.added] == NODE_NAMES
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `pytest tests/test_graph_tracking.py -v`
Expected: FAIL — `ImportError: cannot import name 'NODE_NAMES' from 'library_agent.graph'`（因為還沒實作）。

- [ ] **Step 3: 實作 `NODE_NAMES`、`_mark_status`、`_init_run`、`_with_tracking`，修改 `build_graph`**

修改 [src/library_agent/graph.py](../../../src/library_agent/graph.py)：

```python
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from library_agent.agents.crawler import crawler_node
from library_agent.agents.discoverer import discoverer_node
from library_agent.agents.librarian import librarian_node
from library_agent.agents.parser import parser_node
from library_agent.agents.recommender import recommender_node
from library_agent.agents.validator import validator_node
from library_agent.db.models import Citation
from library_agent.db.models import PipelineNodeRun
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.state import AgentState

# 固定順序，對應 build_graph() 的拓樸；dashboard.py 的 /status 依此順序回傳節點狀態。
NODE_NAMES = [
    "crawler", "parser", "discoverer", "validator",
    "librarian", "human_review", "recommender",
]


def _mark_status(
    run_id: str,
    node_name: str,
    status: str,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """寫一筆節點狀態記錄。每次呼叫用短生命週期的 session，立即 commit。"""
    with SessionLocal() as session:
        session.add(PipelineNodeRun(
            run_id=run_id,
            node_name=node_name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
        ))
        session.commit()


def _init_run(run_id: str) -> None:
    """pipeline 開跑前，為所有節點各寫一筆 pending，讓前端一開始就能畫出完整流程圖。"""
    for name in NODE_NAMES:
        _mark_status(run_id, name, "pending")


def _with_tracking(name: str, fn, run_id: str):
    def wrapped(state: AgentState) -> AgentState:
        _mark_status(run_id, name, "running", started_at=datetime.now(timezone.utc))
        try:
            result = fn(state)
        except Exception as e:
            _mark_status(run_id, name, "error", finished_at=datetime.now(timezone.utc), error=str(e)[:500])
            raise
        _mark_status(run_id, name, "done", finished_at=datetime.now(timezone.utc))
        return result
    return wrapped


def _human_review_node(state: AgentState) -> AgentState:
    """從 DB 讀出被 validator 標記為 requires_human_review 的書目並列出。
    資料來源是 DB（single source of truth），因此獨立重跑 / resume 也能正確列出目前的待審 backlog。"""
    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        q = (
            select(Citation.course_id, Citation.title, Citation.confidence)
            .join(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
            .where(VerifiedBookDB.review_status == "pending")
        )
        if course_ids:
            q = q.where(Citation.course_id.in_(course_ids))
        rows = session.execute(q).all()

    if not rows:
        return {}
    print(f"\n需要人工審核的書目（{len(rows)} 筆）：")
    for course_id, title, confidence in rows:
        print(f"  [{course_id}] {title}  confidence={confidence:.2f}")
    return {}


def build_graph(run_id: str | None = None) -> StateGraph:
    graph = StateGraph(AgentState)

    nodes = {
        "crawler": crawler_node,
        "parser": parser_node,
        "discoverer": discoverer_node,
        "validator": validator_node,
        "librarian": librarian_node,
        "recommender": recommender_node,
        "human_review": _human_review_node,
    }
    for name, fn in nodes.items():
        graph.add_node(name, _with_tracking(name, fn, run_id) if run_id else fn)

    graph.add_edge(START, "crawler")
    graph.add_edge("crawler", "parser")
    graph.add_edge("parser", "discoverer")
    graph.add_edge("discoverer", "validator")

    # validator 之後同時走兩條分支（平行執行）
    graph.add_edge("validator", "librarian")
    graph.add_edge("validator", "human_review")

    graph.add_edge("librarian", "recommender")
    graph.add_edge("recommender", END)
    graph.add_edge("human_review", END)

    return graph.compile()


pipeline = build_graph()
```

- [ ] **Step 4: 執行測試確認通過**

Run: `pytest tests/test_graph_tracking.py -v`
Expected: PASS（4 個測試）

- [ ] **Step 5: 確認既有測試沒有壞掉**

Run: `pytest -v`
Expected: 全部 PASS（既有的 test_crawler、test_validator 等不受影響，因為 `build_graph()` 預設 `run_id=None` 行為不變）。

- [ ] **Step 6: Commit**

```bash
git add src/library_agent/graph.py tests/test_graph_tracking.py
git commit -m "graph.py 加入節點狀態追蹤 wrapper"
```

---

## Task 3: dashboard.py `/status` 回傳節點狀態

**Files:**
- Modify: `src/library_agent/dashboard.py`

**Interfaces:**
- Consumes: `NODE_NAMES`, `build_graph`, `_init_run` from `library_agent.graph`（Task 2 產出）；`PipelineNodeRun` from `library_agent.db.models`（Task 1 產出）。
- Produces: `/status` 回傳 JSON 多一個欄位 `nodes: [{name: str, status: str, elapsed: int|None}, ...]`，順序固定為 `NODE_NAMES`。Task 4 的前端 JS 會讀這個欄位。

- [ ] **Step 1: 修改 `start_run()` 產生 `run_id` 並初始化節點狀態**

在 [src/library_agent/dashboard.py](../../../src/library_agent/dashboard.py) 找到 `_run_state` 全域變數定義（約 line 324），加入 `run_id` 欄位：

```python
_run_lock = threading.Lock()
_run_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "error": None, "run_id": None}
```

修改 `_run_pipeline` 簽名接收 `run_id`：

```python
def _run_pipeline(run_id: str, limit: int | None) -> None:
    from library_agent.graph import build_graph
    try:
        build_graph(run_id).invoke({"limit": limit} if limit else {})
        status, error = "done", None
    except Exception as e:  # 背景執行緒的例外要自己接，否則靜默消失
        status, error = "error", str(e)[:500]
    with _run_lock:
        _run_state.update(status=status, finished_at=time.time(), error=error)
```

修改 `start_run()`：

```python
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
```

- [ ] **Step 2: 修改 `/status` 端點查詢節點狀態**

修改 `status()` 函式：

```python
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
```

- [ ] **Step 3: 手動驗證端點回傳格式**

Run: `python -m uvicorn library_agent.dashboard:app --port 8765 &` (背景啟動，或用既有的 `library-agent dashboard` 指令)
Run: `curl http://127.0.0.1:8765/status`
Expected: JSON 包含 `"nodes":[{"name":"crawler","status":"pending","elapsed":null},...]`（7 筆，因為還沒跑過 pipeline 所以 `run_id` 是 None，`nodes` 應全部是 pending）。

停掉背景程序後再繼續。

- [ ] **Step 4: 執行既有測試確認沒壞**

Run: `pytest -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/library_agent/dashboard.py
git commit -m "dashboard /status 回傳各節點執行狀態"
```

---

## Task 4: 前端 Mermaid 流程圖

**Files:**
- Create: `src/library_agent/static/mermaid.min.js`
- Modify: `src/library_agent/dashboard.py`

**Interfaces:**
- Consumes: `/status` 回傳的 `nodes` 陣列（Task 3 產出，格式 `[{name, status, elapsed}, ...]`）。

- [ ] **Step 1: 下載 Mermaid.js 到本地**

Run:
```bash
mkdir -p src/library_agent/static
curl -sL https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js -o src/library_agent/static/mermaid.min.js
```

Expected: 檔案下載成功，`ls -la src/library_agent/static/mermaid.min.js` 顯示檔案存在且大小 > 0（通常數百 KB）。

這是唯一一次允許碰網路/CDN 的步驟——目的是把檔案取下來 vendor 進專案，之後 dashboard 只從本地 `/static` 路徑載入，不再連外。

- [ ] **Step 2: 掛載 static 目錄 + 加入 Mermaid script tag**

在 [src/library_agent/dashboard.py](../../../src/library_agent/dashboard.py) 檔案開頭 import 區塊加入：

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles
```

在 `app = FastAPI(...)` 之後加入：

```python
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
```

- [ ] **Step 3: 在 `_render()` 的 HTML 加入流程圖卡片與 Mermaid 初始化**

在 [dashboard.py](../../../src/library_agent/dashboard.py) 的 `<head>` 區塊加入 script 標籤（緊接在 `<title>` 之後）：

```html
<script src="/static/mermaid.min.js"></script>
```

在 `.runbar` 卡片（執行 pipeline 按鈕那個 card）**之後**、`.kpi-grid` **之前**加入新卡片：

```html
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
```

- [ ] **Step 4: 加入初始化與上色 JS**

在既有 `<script>` 區塊（`_poll()` 函式所在處）加入 Mermaid 初始化與節點上色邏輯。修改 `<script>` 開頭加入：

```javascript
mermaid.initialize({ startOnLoad: true, theme: 'neutral', securityLevel: 'loose' });

const _NODE_COLOR = { pending: '#898781', running: '#fab219', done: '#0ca30c', error: '#d03b3b' };

function _updateFlowchart(nodes) {
  const svg = document.querySelector('#flowchart svg');
  if (!svg) return;  // Mermaid 尚未完成首次渲染
  nodes.forEach(function(n) {
    const g = svg.querySelector(`[id*="flowchart-${n.name}-"]`);
    if (!g) return;
    const shape = g.querySelector('rect, polygon, .node-bkg') || g.querySelector('*');
    if (shape) shape.style.fill = _NODE_COLOR[n.status] || _NODE_COLOR.pending;
  });
}
```

修改既有 `_poll()` 函式，在更新 `runstatus` 之後加一行呼叫 `_updateFlowchart`：

```javascript
async function _poll() {
  try {
    const d = await (await fetch('/status')).json();
    const c = d.counts;
    _el('counts').textContent =
      `課程 ${c.courses} · 書目 ${c.citations} · 已驗證 ${c.verified} · 館藏 ${c.holdings} · 建議 ${c.recommendations}`;
    const label = {idle:'閒置中', running:`執行中… ${d.elapsed||0}s`, done:`完成（${d.elapsed||0}s）`, error:'錯誤：'+(d.error||'')};
    _el('runstatus').textContent = label[d.status] || d.status;
    _el('runstatus').className = 'runstatus ' + d.status;
    _el('runbtn').disabled = (d.status === 'running');
    if (d.nodes) _updateFlowchart(d.nodes);
    if (d.status === 'running') setTimeout(_poll, 3000);
  } catch (e) { _el('runstatus').textContent = '無法連線'; }
}
```

注意：`_poll()` 呼叫時機在 Mermaid 完成渲染前可能執行，`_updateFlowchart` 已用 `if (!svg) return` 防呆；第一次 `/status` 若 Mermaid 還沒把 `<pre class="mermaid">` 轉成 `<svg>`，這次上色會跳過，下一次輪詢（3 秒後）會補上。若使用者按下「執行 pipeline」立即觸發 `_poll()` 且此時 SVG 未就緒，最多延遲 3 秒才看到顏色，可接受。

- [ ] **Step 5: 手動驗證流程圖顯示與上色**

Run: `python -m uvicorn library_agent.dashboard:app --port 8765` (前景執行，方便看 log)
用瀏覽器開 `http://127.0.0.1:8765/`
Expected:
1. 頁面顯示「Pipeline 進度」卡片，7 個節點的流程圖，含 validator 後分兩條線到 librarian 和 human_review。
2. 開啟瀏覽器 DevTools Network，確認 `mermaid.min.js` 是從 `/static/mermaid.min.js` 載入（本地），沒有對外部網域的請求。
3. 在「限制筆數」填 `2`，按「▶ 執行 pipeline」。
4. 觀察流程圖節點顏色隨時間變化：crawler 先變黃（running）、變綠（done），接著 parser 依序變化，最後 validator 完成後 librarian 與 human_review 應**同時**變黃。
5. Pipeline 完成後所有節點應為綠色（或 human_review 若無 pending 書目，也走完應為綠色）。

按 Ctrl+C 停止伺服器。

- [ ] **Step 6: 確認既有測試沒壞**

Run: `pytest -v`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add src/library_agent/static/mermaid.min.js src/library_agent/dashboard.py
git commit -m "dashboard 加入 Mermaid 流程圖即時上色"
```

---

## Self-Review Notes

- **Spec coverage**：DB 表（Task 1）、graph.py wrapper（Task 2）、`/status` 節點狀態（Task 3）、Mermaid 前端 vendor + 上色（Task 4）、輪詢沿用 3 秒（Task 4 Step 4）、平行分支驗證（Task 4 Step 5）——spec 各節均有對應任務。
- **Placeholder scan**：所有 code block 均為可直接執行的完整內容，無 TBD。
- **Type consistency**：`NODE_NAMES`（Task 2）與 `/status` 回傳的 `nodes` 順序（Task 3）、前端 `_NODE_COLOR` 的 key（Task 4）三處的節點名稱字串一致（`crawler, parser, discoverer, validator, librarian, human_review, recommender`）。`_mark_status` 簽名在 Task 2 定義，Task 2 內 `_with_tracking` 呼叫方式一致。
