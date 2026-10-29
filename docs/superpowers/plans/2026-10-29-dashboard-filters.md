# Dashboard 類別篩選與學系篩選同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dashboard 加上「指定 / 參考 / AI 推薦」類別篩選，並讓學系篩選同步 KPI、圖表、明細表（顯示該學系所有已分析的資料）。

**Architecture:** 類別篩選是純前端：每列帶 `data-btypes` 屬性，與優先級按鈕取交集。學系篩選改成後端過濾：`/` 接受重複的 `departments` 查詢參數，`_load_data` 把每個計數與明細都經 `Course.department` 限縮；前端選擇器從網址還原勾選、關閉面板時若選擇有變就導向新網址。

**Tech Stack:** Python、SQLAlchemy、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（含 `fastapi.testclient`）。

**Spec:** `docs/superpowers/specs/2026-10-29-dashboard-user-feedback-design.md`（本計畫做第 2、1 點）

## Global Constraints

- dashboard 的 HTML 是 f-string，CSS/JS 的大括號一律寫成雙括號 `{{ }}`。
- 類別判定沿用現有規則：AI 推薦 > 指定 > 參考（`raw_mention` 含 `AI推薦` 者為 AI 推薦）。
- 優先級按鈕與類別按鈕可同時選，結果取交集；搜尋框有字時只看搜尋、無視兩組按鈕（現有行為，維持）。
- 學系按鈕與「按執行時分析哪些課」共用同一個控制項；篩選以**系所名**（`/departments` 回傳的名稱）傳遞。
- 學系篩選要同步 KPI、優先級圖、館藏狀態圖、採購建議明細、待審核明細。執行列上的即時筆數（`/status` 的 `counts`）是 pipeline 進度，維持全庫。
- 沿用 `db/filters.py` 的 `purchasable()`，配錯且未經人工放行的書不得進採購建議。
- 現有 122 個測試維持通過。

## Review Focus

- 學系的 `Course.department` 為 `NULL` 或不在 `DEPARTMENT_MAP`：未篩選時照常出現；選任何學系時不出現；未知名稱原樣當原始值比對。→ Task 2
- 選了一個沒有任何已分析資料的學系：各計數為 0、明細顯示「尚無採購建議」空表提示，不得出錯。→ Task 2
- 學系全選：網址不帶 `departments`，資料與未篩選相同。→ Task 3（手動）
- 學系全不選：不導向、畫面維持原樣；執行鈕仍擋「請至少勾選一個學系」。→ Task 3（手動）
- pipeline 跑完後頁面的 `location.reload()`：網址帶著篩選，重整後篩選仍在。→ Task 3（手動）
- 學系名稱含中文與特殊字元：以 `URLSearchParams` 編碼、後端以重複參數解碼，不用逗號串接。→ Task 2、Task 3

## File Structure

- Modify `src/library_agent/dashboard.py`：類別 token 與按鈕、`_expand_departments`、`_load_data` / `index` 的 `departments`、前端篩選與選擇器。
- Create `tests/test_dashboard_filters.py`

---

### Task 1: 類別篩選

**Files:**
- Modify: `src/library_agent/dashboard.py`（`_book_type_label`、`_rec_row`、`_rec_table`、`_render` 的按鈕區與 JS `_applyTableView`）
- Test: `tests/test_dashboard_filters.py`

**Interfaces:**
- Produces:
  - `_book_type_key(is_required: bool, raw_mention: str | None) -> str`：回 `"ai"` / `"required"` / `"optional"`；`_book_type_label` 改由它決定，輸出的 HTML 不變。
  - `_rec_row(priority, course, title, btype, status_html, copies_html, rationale, btypes: str) -> str`：`<tr>` 多一個 `data-btypes="<btypes>"`，值為以空白分隔的 token 清單（現在永遠只有一個；第 4 點合併課程後會有多個，前端不必再改）。
  - `_category_btns() -> str`：按鈕 `data-c` 依序為 `all`（全部）、`required`（指定）、`optional`（參考）、`ai`（AI 推薦），第一個帶 `active`，樣式 class 為 `cbtn`。
  - JS：以 `_applyRecFilters()` 取代 `_applyTableView(tableId, dataKey, btnSel, searchId)`（該函式只有 `rectable` 在用，`data-sf` 分支是死碼，一併移除）。

- [ ] **Step 1: 寫失敗的測試** `tests/test_dashboard_filters.py`：
  - `test_book_type_key`：`_book_type_key(True, "指定") == "required"`、`(False, "參考") == "optional"`、`(True, "[AI推薦] 理由") == "ai"`（AI 優先於指定）、`(False, None) == "optional"`。
  - `test_rec_row_carries_category_token`：`'data-btypes="required"' in _rec_row("high","課","書","<b>",'s',"1","r","required")`。
  - `test_rec_table_rows_have_category_tokens`：`_rec_table` 給一筆 `is_required=True, raw_mention="指定"` 的建議與一筆 `raw_mention="[AI推薦]"` 的待審，輸出含 `data-btypes="required"` 與 `data-btypes="ai"`。
  - `test_category_buttons_sit_beside_priority_buttons`：`_render(_load_data())` 含 `data-c="all"`、`data-c="required"`、`data-c="optional"`、`data-c="ai"`，且 `html.index('data-f="all"') < html.index('data-c="all"')`。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_dashboard_filters.py -v`，預期 4 個 FAIL（`_book_type_key` 不存在、`_rec_row` 少一個參數、頁面沒有 `data-c`）。

- [ ] **Step 3: 實作** 上述四項。按鈕放進現有 `<div class="filters">`，優先級按鈕之後用一個分隔元素再接 `_category_btns()`；`_applyRecFilters()` 讀 `.fbtn.active`、`.cbtn.active` 與搜尋框：搜尋字非空只比課程與書名，否則 `priorityOK && categoryOK`，其中 `categoryOK = c === 'all' || tr.dataset.btypes.split(' ').includes(c)`；兩組按鈕與搜尋框的事件都改呼叫它，`.cbtn` 的 `active` 樣式沿用 `.fbtn.active`。

- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_dashboard_filters.py -v` 4 passed；`python -m pytest -q` 全部通過。

- [ ] **Step 5: Commit**：`git add src/library_agent/dashboard.py tests/test_dashboard_filters.py`，訊息 `feat: dashboard 加類別篩選（指定 / 參考 / AI 推薦）`。

---

### Task 2: 學系篩選的後端

**Files:**
- Modify: `src/library_agent/dashboard.py`（`_load_data`、`index`、`start_run`）
- Test: `tests/test_dashboard_filters.py`

**Interfaces:**
- Produces:
  - `_expand_departments(names: list[str] | None) -> list[str] | None`：`None` 或空清單回 `None`（＝不篩）；否則把系所名展開成原始系級值（用現有 `_DEPT_TO_RAW`，未知名稱原樣保留）。`start_run` 改用它，行為不變。
  - `_load_data(departments: list[str] | None = None) -> dict`：`departments` 是系所名，內部以 `_expand_departments` 展開；`None` 時行為與現在完全相同。
  - `index(departments: list[str] | None = Query(None)) -> str`。
- 限縮範圍：`kpis` 的 `courses` / `citations` / `verified` / `pending`、`prio`、`copies`、`status`、`recs`、`pending` 全部經 `Course.department.in_(原始值)`；只有 `courses` 以外的查詢需要新增對 `Course` 的 join（`recs` 與 `pending` 已經 join）。

- [ ] **Step 1: 寫失敗的測試**（沿用 `tests/test_graph_routing.py` 的真實 DB 播種與清理；以三個專用課程 `TESTFA`、`TESTFB`、`TESTFC`，`department` 依序為 `"TESTDEPT_A"`、`"測試學系_B"`（刻意用中文，涵蓋網址編碼）、`None`，各播種 Course / Citation / VerifiedBook / HoldingCheck / Recommendation 一整條，書名分別含 `TESTBOOK-A` / `-B` / `-C`）：
  - `test_expand_departments`：`None`→`None`、`[]`→`None`、`["TESTDEPT_A"]`→`["TESTDEPT_A"]`（未知名稱原樣）、取 `DEPARTMENT_MAP` 的任一 `(raw, (col, dep))`，`raw in _expand_departments([dep])`。
  - `test_load_data_scopes_every_section_to_departments`：`_load_data(["TESTDEPT_A"])` 的 `kpis["courses"] == 1`、`kpis["citations"] == 1`，`recs` 恰一筆且書名含 `TESTBOOK-A`、不含 `-B`/`-C`；未篩選的 `_load_data()` 三本書都在。
  - `test_load_data_unknown_department_is_empty`：`_load_data(["NO_SUCH_DEPT"])` 的 `kpis["courses"] == 0`、`recs == []`、`pending == []`，`_render` 該資料不丟例外且輸出含「尚無採購建議」。
  - `test_index_route_filters_by_repeated_departments_param`：`TestClient(app).get("/", params=[("departments","TESTDEPT_A")])` 回 200，內容含 `TESTBOOK-A`、不含 `TESTBOOK-B`；改用 `[("departments","測試學系_B")]` 則相反；同時帶兩個 `departments` 參數則兩者都含；無參數時也都含。
  - 清理放在 `finally`，並確認三個專用課程沒有殘留。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_dashboard_filters.py -v -k "expand or scopes or unknown_department or index_route"`，預期 FAIL（`_expand_departments` 不存在、`_load_data` 不接受參數）。

- [ ] **Step 3: 實作** 上述介面；`start_run` 內原本的展開迴圈換成 `_expand_departments(dept_names)`。

- [ ] **Step 4: 執行確認通過**：同上指令 PASS；`python -m pytest -q` 全部通過。

- [ ] **Step 5: Commit**：`feat: 學系篩選同步 KPI、圖表與明細（後端依學系限縮資料）`。

---

### Task 3: 學系選擇器（前端）

**Files:**
- Modify: `src/library_agent/dashboard.py`（JS：`_loadDepts`、選擇器關閉處理）

**Interfaces:**
- Consumes: `index` 的重複 `departments` 參數（Task 2）；現有 `/departments`、`_collectChecked`、`_updateDeptLabel`。
- 行為：
  - 載入時以 `new URLSearchParams(location.search).getAll('departments')` 還原勾選：沒有參數＝全勾；有參數＝只勾名單內的，學院框隨其底下系所是否全勾連動。
  - 面板關閉時（點面板外）比對「目前勾選」與「頁面載入時的勾選」；有變且至少勾一個才導向：全勾導向 `location.pathname`（不帶參數），否則導向帶重複 `departments` 的網址。全不勾不導向。
  - 「執行」按鈕沿用同一組勾選（現有 `_collectChecked('.deptchk')`），不改。

- [ ] **Step 1: 實作** 上述行為。這是純瀏覽器行為，repo 沒有 JS 測試環境，不寫自動測試。

- [ ] **Step 2: 手動驗證**：`python -m library_agent.main dashboard`，開 http://127.0.0.1:8000。預期：
  - 只勾「國際關係」後點面板外：網址變成 `/?departments=...`，KPI、兩張圖、明細表都只剩該學系，學系按鈕顯示「1/N」。
  - 全選後點面板外：網址回到 `/`，資料回到全庫。
  - 全不選後點面板外：畫面不動，按「執行」跳出「請至少勾選一個學系」。
  - 篩選後按重新整理：篩選仍在、勾選狀態與網址一致。
  - 類別按鈕與優先級按鈕同時選，結果取交集；搜尋框輸入文字時只看搜尋。

- [ ] **Step 3: Commit**：`feat: 學系選擇器與網址同步，關閉面板時套用篩選`。
