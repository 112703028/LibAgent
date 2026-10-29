> **已被取代（2026-10-29）**：這份計畫實作的「每個節點一列、已處理 X / 共 Y 本、進度條」版本，使用者看過後改要終端機文字 log，該實作已移除。最終設計見設計文件第 5 點。保留此檔作為決策過程的紀錄。

# Dashboard 處理進度 log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用「各節點已處理 X / 共 Y 本 + 最新一行訊息」的進度區，取代 dashboard 的 mermaid 流程圖。

**Architecture:** 新增 `progress.py`：`graph._with_tracking` 在節點執行期間用 contextvar 綁定 (run_id, node)，節點迴圈內呼叫 `report(processed, total, message, unit)`，節流後更新該節點在 `pipeline_node_runs` 的最新一列。`/status` 把這些欄位帶給前端，前端既有的輪詢更新進度列。

**Tech Stack:** Python、SQLAlchemy + Alembic（PostgreSQL）、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest。

**Spec:** `docs/superpowers/specs/2026-10-29-dashboard-user-feedback-design.md`（本計畫只做第 5 點）

## Global Constraints

- dashboard 的 HTML 是 f-string，CSS/JS 的大括號一律寫成雙括號 `{{ }}`。
- 進度區節點順序與 `graph.NODE_NAMES` 一致。
- 節點狀態色沿用 `dashboard._NODE_STATUS_META`（不再另寫一份 hex）。
- 進度回報是盡力而為：任何失敗都不得讓 pipeline 中斷。
- 沒有綁定 run 時（例如單獨執行 `python agents/xxx.py`）`report` 必須是 no-op。
- 「最新一行訊息」只保留每個節點最後一行，不存歷史。
- 時間預估不做。
- 現有 97 個測試維持通過。

## Review Focus

- `total` 為 0 或未回報（crawler、human_review、沒有待辦的節點）：進度列顯示「—」，進度條不得除以零。→ Task 4 `_pct`
- 單獨執行節點、沒有綁定 run：`report` 靜默跳過。→ Task 1
- 節點中途丟例外：綁定必須解除、狀態為 error，最後一次進度保留。→ Task 2
- 訊息很長（書名、錯誤字串）：截斷，不得撐爆版面或資料庫欄位。→ Task 1
- 進度寫入失敗（DB 暫時斷線）：不影響節點本身。→ Task 1
- discoverer 在迴路裡會被執行多次：每次都是新的一列，`report` 只能更新「該節點最新一列」。→ Task 1

## File Structure

- Create `src/library_agent/progress.py`：綁定與回報，無其他職責。
- Create `alembic/versions/f4a7b3d1e9c2_add_progress_to_pipeline_node_runs.py`
- Modify `src/library_agent/db/models.py`：`PipelineNodeRun` 加四個欄位。
- Modify `src/library_agent/graph.py`：`_with_tracking` 綁定/解除。
- Modify `src/library_agent/agents/{match_auditor,parser,discoverer,validator,librarian,recommender}.py`：迴圈內加 `report`。
- Modify `src/library_agent/dashboard.py`：`/status`、進度卡片、移除流程圖與 `/static`。
- Delete `src/library_agent/static/mermaid.min.js`
- Create `tests/test_progress.py`、`tests/test_match_auditor_progress.py`、`tests/test_dashboard_progress.py`；Modify `tests/test_graph_tracking.py`。

---

### Task 1: progress 模組與 schema

**Files:**
- Create: `src/library_agent/progress.py`
- Create: `alembic/versions/f4a7b3d1e9c2_add_progress_to_pipeline_node_runs.py`
- Modify: `src/library_agent/db/models.py`（`PipelineNodeRun`）
- Test: `tests/test_progress.py`

**Interfaces:**
- Produces:
  - `PipelineNodeRun.processed: int | None`、`.total: int | None`、`.unit: str | None`（String(10)）、`.message: str | None`（Text）。Migration `down_revision = 'e3f6a2c9d8b1'`，四欄皆 nullable，downgrade 反向移除。
  - `progress._Binding`（dataclass：`run_id: str`、`node: str`、`last_write: float | None = None`）
  - `progress._current: ContextVar[_Binding | None]`（預設 `None`）
  - `progress.bind(run_id: str, node: str) -> contextvars.Token`
  - `progress.unbind(token: contextvars.Token) -> None`
  - `progress.report(processed: int, total: int, message: str | None = None, unit: str = "本") -> None`
  - `progress._now`（`time.monotonic`，測試用 monkeypatch 替換；`report` 每次呼叫**恰好**呼叫一次 `_now()`）、`progress._MIN_INTERVAL = 0.5`、`progress._MAX_MESSAGE = 200`

- [ ] **Step 1: 寫失敗的測試** `tests/test_progress.py`。用 `_FakeSession`（`__enter__/__exit__`、`execute(stmt)` 把 `stmt.compile()` 存進 list、`commit()`）monkeypatch `library_agent.progress.SessionLocal`。測試名稱與斷言：
  - `test_report_without_binding_is_noop`：`SessionLocal` 換成一被呼叫就 `AssertionError` 的函式，`report(1, 2, "x")` 不得丟例外。
  - `test_first_report_writes_all_fields`：`bind("run1","validator")` 後 `report(1, 10, "書A", "本")`，`compiled.params` 含 `processed == 1`、`total == 10`、`unit == "本"`、`message == "書A"`。
  - `test_report_targets_latest_row_of_run_and_node`：同一份 compiled 的 `str(compiled).lower()` 含 `max(`，`params` 含 `run1` 與 `validator`。
  - `test_reports_within_interval_are_throttled`：`_now` 依序回 `0.0, 0.1, 0.2`，三次 `report` 只寫 1 次。
  - `test_report_after_interval_writes_again`：`_now` 回 `0.0, 0.6`，寫 2 次。
  - `test_final_report_always_written`：`_now` 回 `0.0, 0.1`，`report(1,10)` 後 `report(10,10)`，寫 2 次。
  - `test_long_message_is_truncated`：`"x"*500` → `len(params["message"]) == progress._MAX_MESSAGE`。
  - `test_db_failure_does_not_raise`：`execute` 丟 `RuntimeError`，`report` 不得丟出。
  - `test_unbind_stops_writes`：`unbind` 後 `report` 不寫。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_progress.py -v`，預期 `ImportError`/`AttributeError`（模組不存在）。

- [ ] **Step 3: 實作** `progress.py` 與 model/migration。`report` 先查 `_current.get()`，無綁定直接 return；用 `update(PipelineNodeRun).where(PipelineNodeRun.id == select(func.max(PipelineNodeRun.id)).where(run_id 與 node_name 相符).scalar_subquery())` 寫入；節流規則：`last_write is None`、或 `processed >= total`、或 `_now() - last_write >= _MIN_INTERVAL` 才寫；整個寫入包 `try/except Exception` 吞掉。訊息超長時截成剛好 `_MAX_MESSAGE` 字元。

- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_progress.py -v`，預期 9 passed。再跑 `alembic upgrade head`，預期無錯誤並升到 `f4a7b3d1e9c2`。

- [ ] **Step 5: Commit**：`git add src/library_agent/progress.py src/library_agent/db/models.py alembic/versions/f4a7b3d1e9c2_add_progress_to_pipeline_node_runs.py tests/test_progress.py`，訊息 `feat: progress 模組與 pipeline_node_runs 進度欄位`。

---

### Task 2: `_with_tracking` 綁定進度

**Files:**
- Modify: `src/library_agent/graph.py`（`_with_tracking`）
- Test: `tests/test_graph_tracking.py`

**Interfaces:**
- Consumes: `progress.bind`、`progress.unbind`、`progress._current`（Task 1）

- [ ] **Step 1: 寫失敗的測試**（沿用該檔的 `_FakeSession`）：
  - `test_with_tracking_binds_progress_during_fn_and_unbinds_after`：節點函式內讀 `progress._current.get()`，斷言 `.run_id == "run1"`、`.node == "validator"`；`wrapped({})` 回來後 `progress._current.get() is None`。
  - `test_with_tracking_unbinds_even_when_fn_raises`：節點丟 `ValueError`，`pytest.raises` 之後 `progress._current.get() is None`，且 `fake.added[-1].status == "error"`。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_graph_tracking.py -v`，預期上述兩個 FAIL。

- [ ] **Step 3: 實作**：在 `wrapped` 內 `fn(state)` 之前 `token = progress.bind(run_id, name)`，用 `try/finally` 保證 `progress.unbind(token)`；既有的 running/done/error 狀態標記行為不變。

- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_graph_tracking.py -v`，預期全數 PASS。

- [ ] **Step 5: Commit**：`feat: _with_tracking 在節點執行期間綁定進度回報`。

---

### Task 3: 節點回報進度

**Files:**
- Modify: `src/library_agent/agents/match_auditor.py`、`parser.py`、`discoverer.py`、`validator.py`、`librarian.py`、`recommender.py`
- Test: `tests/test_match_auditor_progress.py`

**Interfaces:**
- Consumes: `progress.report`（各檔以 `from library_agent.progress import report` 引入，測試才能 monkeypatch `<module>.report`）
- 語意：`processed` 是「處理到第幾筆」，`total` 是本次要處理的總數，`message` 是目前這筆的簡短名稱。

各節點回報位置（單位 `unit`）：

| 節點 | 位置 | processed / total | message | unit |
|---|---|---|---|---|
| parser | `as_completed` 迴圈 `done += 1` 之後、`if error` 之前 | `done` / `total` | `f"{course.course_id} {course.course_name}"` | `"門課"` |
| discoverer | 初次模式 `for i, course in enumerate(courses, 1)` 迴圈頂端；重推模式的課程迴圈改成 `enumerate(courses, 1)` 並在頂端回報 | `i` / `total`（重推為 `len(courses)`） | `course.course_name` | `"門課"` |
| validator | `as_completed` 迴圈 `done += 1` 之後 | `done` / `total` | `c.title` | `"本"` |
| match_auditor | 規則層寫入 commit 後 `report(len(rule_mismatch), len(items), "規則層完成")`；LLM 迴圈每筆 `_apply_verdict` 之後 | `len(rule_mismatch) + n` / `len(items)` | `it["orig_title"]` | `"本"` |
| librarian | `as_completed` 迴圈 `done += 1` 之後 | `done` / `total` | `vb.canonical_title` | `"本"` |
| recommender | `enumerate(rows, 1)` 迴圈頂端 | `i` / `total` | `citation.title` | `"本"` |

- [ ] **Step 1: 寫失敗的測試** `tests/test_match_auditor_progress.py`（沿用 `tests/test_graph_routing.py` 的真實 DB 播種與清理模式，課程代號 `TESTAUDPROG`）。播種 2 筆已驗證書目：一筆書名完全不相干（規則層直接判配錯）、一筆書名相同（走 LLM）。`monkeypatch` `match_auditor._llm_same_book` 回 `(True, "ok")`、`openai.OpenAI` 換成 `lambda **kw: object()`、`match_auditor.report` 換成記錄呼叫的函式。測試 `test_reports_progress_over_all_items`：`match_auditor_node({"course_ids": ["TESTAUDPROG"]})` 之後，每次呼叫的 `total == 2`、`processed` 單調不減、最後一次 `processed == 2`。`finally` 清掉播種資料。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_match_auditor_progress.py -v`，預期 FAIL（`match_auditor` 沒有 `report`）。

- [ ] **Step 3: 實作** 依上表在六個節點加入回報；其餘邏輯不動。

- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_match_auditor_progress.py -v` PASS；再 `python -m pytest -q` 確認全部通過；用 `grep -n "report(" src/library_agent/agents/*.py` 確認六個檔案各有回報呼叫。

- [ ] **Step 5: Commit**：`feat: 各節點回報處理進度`。

---

### Task 4: `/status` 帶出進度、進度卡片 HTML

**Files:**
- Modify: `src/library_agent/dashboard.py`
- Test: `tests/test_dashboard_progress.py`

**Interfaces:**
- Produces:
  - `dashboard._NODE_LABELS: dict[str, str]`：`crawler→爬取課綱`、`parser→解析書目`、`discoverer→AI 推薦補書`、`validator→書目驗證`、`match_auditor→配對稽核`、`librarian→館藏查詢`、`recommender→採購建議`、`human_review→人工審核`。
  - `dashboard._pct(processed: int | None, total: int | None) -> int`：`total` 為 `None`/`0` 回 `0`；否則 `min(100, round(processed / total * 100))`。
  - `dashboard._progress_card(node_names: list[str]) -> str`：每個節點一列 `<div class="prow" data-node="<name>">`，內含 `.pstatus`（狀態色點）、`.pname`（名稱＋中文說明）、`.pbar > span`（進度條）、`.pcount`（「X / Y 本」，無資料顯示「—」）、`.pmsg`（最新訊息）。初始狀態全為「未開始」。
  - `/status` 的 `nodes` 每項改為 `{"name", "status", "processed", "total", "unit", "message"}`，取自該節點在本次 run 的**最新一列**。

- [ ] **Step 1: 寫失敗的測試** `tests/test_dashboard_progress.py`：
  - `test_pct_handles_zero_and_missing_total`：`_pct(None, None) == 0`、`_pct(3, 0) == 0`、`_pct(5, 10) == 50`、`_pct(12, 10) == 100`。
  - `test_progress_card_has_one_row_per_node_in_order`：`_progress_card(["crawler","parser"])` 中 `data-node="crawler"` 出現在 `data-node="parser"` 之前，且沒有其他節點列。
  - `test_progress_card_without_data_shows_dash`：回傳的 HTML 含 `—`。
  - `test_status_returns_latest_progress_per_node`：真實 DB 播種 `PipelineNodeRun`（`run_id="TESTPROGRUN"`，validator 兩列：較舊 `processed=1,total=10`、較新 `processed=7,total=10,unit="本",message="書B"`），把 `dashboard._run_state["run_id"]` 設成該值，呼叫 `dashboard.status()`，斷言 validator 項目為 `processed == 7`、`total == 10`、`message == "書B"`，其餘節點 `processed is None`；`finally` 清掉播種資料並還原 `_run_state`。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_dashboard_progress.py -v`，預期 FAIL。

- [ ] **Step 3: 實作** 上述四項。`/status` 的查詢改成取每個節點最新一列的完整欄位（依 `id` 由新到舊，每個節點只取第一筆）。

- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_dashboard_progress.py -v`，預期 4 passed。

- [ ] **Step 5: Commit**：`feat: /status 帶出節點進度與進度卡片 HTML`。

---

### Task 5: 以進度區取代流程圖

**Files:**
- Modify: `src/library_agent/dashboard.py`（`_render`、CSS、JS）
- Delete: `src/library_agent/static/mermaid.min.js`
- Test: `tests/test_dashboard_progress.py`

**Interfaces:**
- Consumes: `_progress_card`、`_NODE_STATUS_META`、`_node_color_js`、`_flow_legend`、`/status` 新格式（Task 4）

- [ ] **Step 1: 寫失敗的測試**：
  - `test_render_has_progress_rows_and_no_mermaid`：`html = _render(_load_data())`，斷言對 `graph.NODE_NAMES` 每個名稱 `f'data-node="{name}"' in html`，且 `"mermaid" not in html`、`"/static/" not in html`。
  - `test_app_has_no_static_mount`：`[r.path for r in dashboard.app.routes]` 不含 `/static`。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_dashboard_progress.py -v`，預期這兩個 FAIL。

- [ ] **Step 3: 實作**：
  - `_render`：「Pipeline 進度」卡片內容換成 `_progress_card(NODE_NAMES)`，保留 `_flow_legend()` 圖例；刪除 `<script src="/static/mermaid.min.js">`、`<pre class="mermaid">`、`mermaid.initialize(...)` 與其註解、`_updateFlowchart`、`.mermaid` 的 CSS。
  - 新增 JS `_updateProgress(nodes)`：依 `data-node` 找列，更新 `.pstatus` 底色（用 `_NODE_COLOR`）、`.pbar > span` 的寬度、`.pcount`（`processed`/`total` 為 `null` 顯示「—」）、`.pmsg`；輪詢處把 `_updateFlowchart(d.nodes)` 換成 `_updateProgress(d.nodes)`。
  - 圖例下方說明文字改為：「validator 之後若有 AI 推薦書驗證失敗，會回到 discoverer 重推（最多 `{_MAX_DISCOVERY_ITERATIONS - 1}` 輪）；match_auditor 會把配對可疑或信心度不足的書送進 human_review，並排除在採購建議之外。」
  - 刪除 `app.mount("/static", ...)` 與 `StaticFiles` 的 import；`git rm src/library_agent/static/mermaid.min.js`。

- [ ] **Step 4: 執行確認通過**：`python -m pytest -q` 全部通過；`python -c "from library_agent.dashboard import _render, _load_data; _render(_load_data())"` 無錯誤。

- [ ] **Step 5: 手動驗證**：`python -m library_agent.main dashboard`，開 http://127.0.0.1:8000，在學系篩選選一個小學系後按執行。預期：進度區 8 列依序出現；跑到 parser、validator、match_auditor、librarian、recommender 時，該列的「X / Y」與最新訊息會動，完成後狀態色變綠；crawler 與 human_review 只顯示狀態；頁面沒有流程圖。

- [ ] **Step 6: Commit**：`feat: dashboard 以處理進度區取代 mermaid 流程圖`。
