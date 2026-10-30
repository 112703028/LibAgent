# 單筆追溯畫面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 點採購建議明細的一列，展開這本書的決策過程：課綱原文 → 解析 → 配對並排 → Alma 館藏 → 冊數怎麼算。

**Architecture:** 新模組 `trace_view`：`build_trace(session, rec_id)` 蒐集資料（重算冊數時重用 `recommender.merge_group`），`render_trace(trace)` 產生已逸出的 HTML 片段；dashboard 新增 `GET /trace/{id}`；前端第一次展開時才取片段、插成明細列下方的一列。

**Tech Stack:** Python、SQLAlchemy、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（獨立測試資料庫 `isolated_db`）、jsdom 驗證瀏覽器行為。

**Spec:** `docs/superpowers/specs/2026-10-29-dashboard-user-feedback-design.md`（第 3 點）

## Global Constraints

- 只讀：不寫資料庫、不在畫面上核准或退回。
- **課綱、書名、作者都是第三方文字：進 HTML 一律 `html.escape`**；前端只把伺服器逸出後的片段設成 innerHTML，不在 JS 裡拼資料。
- 重算冊數必須與 `recommender._decide` 一致：解釋用的中間值（需求、缺口）要有測試鎖住「與 `merge_group` 的結果相同」，避免說明與實際算式分家。
- 重算用 `recommendation_courses.enrolled`（計算當時的人數），不用課程現在的人數，結果才與資料庫存的建議可比。
- 展開鈕不得有文字內容（用 CSS `::before` 畫箭頭）：匯出 CSV 是讀每格的 `textContent`，有文字會污染資料。
- 展開的明細列（`tr.trace-row`）不得被篩選、搜尋、CSV 匯出當成資料列處理。
- HTML 是 f-string：JS/CSS 大括號寫成雙括號；**含反斜線的程式碼一律用 Write/Edit 工具寫入**（Bash heredoc 會把雙反斜線收成單個）。
- 在 `isolated_db` 以外的測試只能讀開發庫、播種專用資料並清掉。
- `git add` 一律列明確路徑；`dashboard.py` 有使用者未提交的行尾空白（`_collectChecked` 附近），用 `scratchpad/stage_mine.py` 只暫存自己的區塊。
- 現有 182 個測試維持通過。

## Review Focus

- 課綱含 `<script>`、書名含 `<` 或引號：不得變成可執行的 HTML。→ Task 1
- 課綱極長（最長 32454 字）：截斷並標明原長度，片段不得爆量。→ Task 1
- 資料庫存的建議與重算結果不同：明確標出差異。→ Task 1
- 沒有關聯列的舊格式建議、找不到的 id、書目缺館藏：不得丟 500。→ Task 1、2
- 展開明細列後：優先級／類別篩選、搜尋、全選、匯出 CSV 都不得把它當資料列。→ Task 3
- 同一列重複點：只取一次、收合再展開不重取。→ Task 3

## File Structure

- Create `src/library_agent/trace_view.py`；Test `tests/test_trace_view.py`
- Modify `src/library_agent/dashboard.py`（`RecRow.rec_id`、`_rec_row`、`GET /trace/{id}`、CSS、JS）；Test `tests/test_dashboard_dedup.py`

---

### Task 1: 蒐集與渲染（`trace_view`）

**Files:** Create `src/library_agent/trace_view.py`；Test `tests/test_trace_view.py`（用 `isolated_db` 播種）

**Interfaces:**（全部 `@dataclass(frozen=True)`）
- `Syllabus(course_id, course_name, semester, text, length)`：`text` 截到 `MAX_SYLLABUS = 6000` 字元，`length` 是原長度。
- `SourceItem(course_id, course_name, citation_id, mention, title, authors: list[str], edition, isbn, publisher, year, kind, confidence, enrolled)`：`kind ∈ required|optional|ai`（沿用 `dashboard` 的判定規則：`raw_mention` 含 `AI推薦` 為 ai）。
- `Pairing(matched_title, matched_authors: list[str], isbn, source, verdict, judge, reason, review_status, originals: list[tuple[str, list[str]]])`：結果完全相同者合併，`originals` 列出各書目原本的 (書名, 作者)。
- `HoldingInfo(status, copies, mms_id, edition, members_differ: bool)`。
- `Calc(courses: list[tuple[str, int]], enrolled_total, per_copy, need, current, gap, cap, is_required, status, priority, copies, stored_priority, stored_copies, consistent: bool)`。
- `Trace(rec_id, title, book_key, rationale, syllabi, sources, pairings, holding: HoldingInfo | None, calc: Calc | None)`。
- `build_trace(session, rec_id: int) -> Trace | None`：找不到回 `None`；有關聯列就依關聯列蒐集，沒有（舊格式）就退回該建議自己的書目；書目沒有館藏的成員略過，全沒有則 `holding` 與 `calc` 為 `None`。重算：由成員組 `recommender.Member` → `merge_group`；`need = ceil(total/per)`（總人數 0 時為 1，與 `_decide` 相同）、`gap = max(need − current, 0)`。
- `render_trace(trace: Trace) -> str`：`<div class="trace">`，五個 `<section class="tr-sec" data-sec="1".."5">` 依序；所有文字 `html.escape`；配錯的配對加 `tr-bad`；不一致時加 `.tr-drift` 說明；課綱完整內容放 `<details>`。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_build_trace_collects_every_source_of_the_book`：兩門課同 ISBN → `sources` 兩筆、`syllabi` 兩份、人數取關聯表快照。
  - `test_syllabus_is_per_course_and_truncated`：課綱 20000 字 → `text` 長度 `MAX_SYLLABUS`、`length == 20000`；同一門課兩個書目只有一份課綱。
  - `test_identical_pairings_are_grouped_and_different_ones_listed`
  - `test_calc_explanation_agrees_with_merge_group`：對多組 (人數, 現有館藏, 上限) 斷言 `calc.gap` 套上限後等於 `calc.copies`、且等於 `merge_group` 的結果（任一筆電子書時 `copies == 0`）。
  - `test_calc_flags_drift_when_stored_numbers_differ`：把建議存的冊數改掉 → `consistent is False` 且 render 出 `tr-drift`。
  - `test_legacy_recommendation_without_links_falls_back_to_its_own_book`、`test_unknown_id_returns_none`、`test_member_without_a_holding_is_skipped`。
  - `test_render_escapes_untrusted_text`：課綱與書名含 `<script>alert(1)</script>`、引號 → 輸出不含未逸出的 `<script>`。
  - `test_render_has_five_sections_in_order`、`test_render_marks_a_mismatch_with_its_reason`。
- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_trace_view.py -q`，預期 ImportError。
- [ ] **Step 3: 實作** `trace_view.py`。
- [ ] **Step 4: 執行確認通過**：同上；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**：`git add src/library_agent/trace_view.py tests/test_trace_view.py`，訊息 `feat: trace_view 蒐集並渲染一本書的決策過程`。

---

### Task 2: 端點、展開鈕與樣式

**Files:** Modify `src/library_agent/dashboard.py`；Test `tests/test_dashboard_dedup.py`

**Interfaces:**
- `RecRow` 新增欄位 `rec_id: int = 0`（放最後，既有建構呼叫不受影響）；`_load_data` 填入。
- `_rec_row(..., course_html=None, rec_id: int | None = None)`：有 `rec_id` 時 `<tr>` 加 `data-rec`，書名格前加 `<button class="tbtn" type="button" aria-expanded="false" aria-label="展開決策過程"></button>`（**不含任何文字**）；待審核列不傳 `rec_id`、沒有按鈕。
- `GET /trace/{rec_id}`：回 `HTMLResponse(render_trace(...))`；找不到回 404。
- CSS：`.tbtn`（箭頭用 `::before`，`aria-expanded="true"` 時換方向）、`.trace-row`、`.trace`、`.tr-sec`、`.tr-bad`、`.tr-drift`。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_trace_endpoint_returns_an_escaped_fragment`（`TestClient`，用現有 `shared_book` 播種取得建議 id）：200、含五個 `data-sec`、含兩門課名稱。
  - `test_trace_endpoint_404_for_unknown_id`。
  - `test_rec_table_rows_carry_rec_id_and_a_text_free_toggle`：有 `data-rec` 與 `class="tbtn"`，且 `<button ...></button>` 之間沒有文字。
  - `test_pending_rows_have_no_toggle`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**：`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（用 `stage_mine.py` 排除使用者的區塊）：`feat: dashboard 加 /trace/{id} 與每列的展開鈕`。

---

### Task 3: 前端展開與既有功能的相容

**Files:** Modify `src/library_agent/dashboard.py`（JS）

**Interfaces:**
- 事件委派於 `#rectable`：點 `.tbtn` → 第一次：在該列後插入 `tr.trace-row`（`td colspan="8"`，先顯示「載入中…」）、`fetch('/trace/' + id)`、成功後 `innerHTML = 片段`；失敗顯示錯誤訊息與重試；之後點只切換 `hidden` 與 `aria-expanded`，不重取。
- `_applyRecFilters`：略過 `tr.trace-row`；展開列的顯示跟隨其上一列（上一列被篩掉就一起隱藏）。
- CSV 匯出與「全選」：只處理 `tr:not(.trace-row)`。

- [ ] **Step 1: 寫 jsdom 驗證腳本（暫存區，不進版本庫）並看它失敗**：載入含展開鈕的頁面，驗證：點按鈕 → 呼叫 `/trace/<id>` 且只一次；展開列出現在該列之後；再點收合、再點展開不重取；優先級／類別篩選時展開列跟著父列顯示或隱藏；搜尋不把展開列當資料列；匯出的 CSV 不含展開列的內容與按鈕文字；取回失敗時顯示錯誤並可重試。
- [ ] **Step 2: 實作** JS（注意雙括號）。
- [ ] **Step 3: 執行確認通過**：jsdom 腳本全過；`python -m pytest -q` 全過。
- [ ] **Step 4: 真實資料核對**：對 `isbn:9780199296095`（政治學 80 人參考＋政治學概論 20 人指定、現有 2 冊）產生追溯片段並讀一遍；重算結果須是 medium、2 本，與資料庫一致。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 採購建議明細可展開查看決策過程`；並在 `CLAUDE.md` 簡述追溯畫面與 `/trace/{id}`。
