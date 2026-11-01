# 採購冊數由使用者決定（第二步）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 採購表每本書多一欄可填的「決定冊數」，系統建議只是參考；被上限截斷時一眼看得出（「需求 N → 建議 M」）；決定存在獨立的小表，重算建議不會洗掉；KPI 與匯出分「系統建議」與「採用數」。

**Architecture:** 新表 `purchase_decisions`（以 `book_key` 為鍵）；把「需求冊數」的算式抽成共用的輕量模組 `demand.py`（recommender、追溯畫面、儀表板共用一份）；`_load_data` 在讀取時算出每本書的「不設上限的需求」並疊上人的決定；新端點 `POST /purchase/decide`；採購表加一欄輸入框。不動 librarian、recommender 的決策流程。

**Tech Stack:** Python、SQLAlchemy 2、Alembic、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（`isolated_db`）、jsdom。

**Spec:** `docs/superpowers/specs/2026-10-31-review-panel-design.md`（「第二步：採購冊數由使用者決定」）

## Global Constraints

- migration 只新增一張表、可 downgrade；現有資料不動。
- 決定**不得存在 `recommendations` 上**（librarian／recommender 會刪掉重建）；以 `book_key` 為鍵存在 `purchase_decisions`。
- 「需求」只用一份算式（`demand.py`）：`need = ceil(總人數 / 每本服務人數)`（總人數為 0 時為 1）；`uncapped = max(need − 現有冊數, 0)`，有電子書時為 0。`recommender._decide`、`trace_view._calc`、儀表板都用它，並有測試鎖住「`min(uncapped, 上限)` 等於 `_decide` 的冊數」。
- 讀取時才算「需求」，不存：上限、館藏、人數變了，顯示永遠與現況一致。
- 決定冊數：整數、0 到 9999；`0`＝這次不買（書仍留在清單，與審核的「退回」不同）；留空／清除＝採用系統建議（刪除該列）。驗證失敗回 400 且不寫入。
- 不記是誰決定（先不做），只記時間與決定當時的系統建議。
- 匯出 CSV 讀採購表每格 `textContent`：輸入框的值與「需求」標記都不是文字節點，匯出程式要**明確**讀 `input.value` 與 `data-*` 屬性；採購表不得因此多出文字。
- 採購表原有欄位順序不變，新欄放在「冊數」之後；搜尋用的欄位索引（課程第 3、書名第 4 欄）不變。
- HTML 是 f-string：JS/CSS 大括號寫成雙括號；**含反斜線的程式碼一律用 Write/Edit 工具寫入**（Bash heredoc 會把雙反斜線收成單個）；shell 的 `-c` 字串裡不要放 Windows 路徑。
- 寫入決定的測試一律用 `isolated_db`，**絕不對開發庫做真實決定**。
- `git add` 一律列明確路徑；`dashboard.py` 可能有使用者未提交的區塊，用 `scratchpad/stage_mine.py`；不提交 `nla.py`、`runlog.py`。
- 現有 339 個測試維持通過；因行為改變而必須改的測試要明確改並在 commit 說明（報表列的鍵會增加）。
- 對開發庫執行 `alembic upgrade head` 在 Task 1（之後 `_load_data` 會查新表，開發庫不升級會讓依賴開發庫的測試失敗），**先備份、並告知使用者**。

## Review Focus

- 重跑 recommender（含刪掉重建建議）後，人的決定仍在、仍對得上同一本書。→ Task 3
- 填 0 與清除（留空）的差別：0＝不買、清除＝採用系統建議；KPI 的採用數與匯出的最終採購數一致。→ Task 2、4
- 非整數、負數、超過 9999、布林、空 book_key、找不到建議、舊格式（`book_key` 為 NULL）：400／404，不寫入；舊格式列不給輸入框。→ Task 3、4
- 決定之後系統建議變了（人數、館藏更新）：標示「建議已從 A 變成 B」，不悄悄沿用。→ Task 2
- 匯出 CSV 帶出「需求冊數／決定冊數／最終採購數」，且不含標記或輸入框的多餘文字。→ Task 4
- `book_key`、冊數進 HTML 屬性：一律逸出。→ Task 4

## File Structure

- Create `src/library_agent/demand.py`；Modify `agents/recommender.py`（`_decide`）、`trace_view.py`（`_calc`）；Test `tests/test_demand.py`
- Modify `db/models.py`；Create `alembic/versions/<新>_add_purchase_decisions.py`；Test `tests/test_purchase_schema.py`
- Modify `dashboard.py`（`RecRow`、`_load_data`、KPI）、`main.py`（報表列與 CSV）；Test `tests/test_dashboard_review.py`（或新檔 `tests/test_dashboard_decisions.py`）、`tests/test_dashboard_dedup.py`
- Modify `dashboard.py`（端點）；Test `tests/test_purchase_endpoint.py`
- Modify `dashboard.py`（採購表欄位、KPI 卡片、JS、CSS）、`CLAUDE.md`

---

### Task 1: 共用的需求算式與 `purchase_decisions` 表

**Files:** Create `demand.py`、migration；Modify `db/models.py`、`agents/recommender.py`、`trace_view.py`；Test `tests/test_demand.py`、`tests/test_purchase_schema.py`（`isolated_db`）

**Interfaces:**
- `demand.need_copies(enrolled_total: int) -> int`：`ceil(enrolled_total / max(students_per_copy, 1))`，`enrolled_total <= 0` 時為 1。
- `demand.uncapped_copies(status: HoldingStatus, enrolled_total: int, current_copies: int) -> int`：電子書為 0；否則 `max(need_copies(enrolled_total) − current_copies, 0)`（**不套上限**）。
- `recommender._decide` 改用上面兩個函式（輸出與行為完全不變）；`trace_view._calc` 的 `need` 改用 `need_copies`。
- `PurchaseDecision`（表 `purchase_decisions`）：`id`、`book_key: String(500)` 唯一非空、`copies: Integer` 非空、`suggested_at_decision: Integer` 非空、`decided_at: DateTime(timezone=True)` 非空。migration `down_revision = "b8d2f4a6c1e3"`。

- [ ] **Step 1: 寫失敗的測試**：
  - `tests/test_demand.py`（純函式）：`test_need_copies_rounds_up_and_treats_zero_enrolment_as_one`、`test_uncapped_copies_ignores_the_cap`（人數 670、現有 1 → 26，而 `_decide` 給 5）、`test_uncapped_is_zero_for_an_ebook_or_when_holdings_cover_the_need`、`test_capped_uncapped_equals_decide_for_a_grid_of_cases`（參數化：狀態 × 人數 × 現有；`min(uncapped, 上限)` 等於 `_decide` 的冊數，電子書時兩者皆 0）。
  - `tests/test_purchase_schema.py`：`test_purchase_decision_roundtrip`、`test_book_key_is_unique`（重複拋 `IntegrityError`）、`test_book_key_and_copies_are_required`。
  - 追溯畫面：既有 `test_calc_explanation_agrees_with_merge_group` 維持通過。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作** `demand.py`、model、migration；`_decide` 與 `_calc` 改用共用函式。
- [ ] **Step 4: 驗證 migration**：用拋棄式資料庫（`library_agent_migtest`，仿先前的 `scratchpad/migcheck.py`）驗證 upgrade → downgrade -1 → upgrade；`python -m pytest -q` 全過。
- [ ] **Step 5: 備份並升級開發庫**：`pg_dump` 到 `~/backups/library_agent_before_purchase_decisions.sql`，再 `alembic upgrade head`；告知使用者。
- [ ] **Step 6: Commit**：`git add src/library_agent/demand.py src/library_agent/db/models.py alembic/versions/<新檔> src/library_agent/agents/recommender.py src/library_agent/trace_view.py tests/test_demand.py tests/test_purchase_schema.py`，訊息 `feat: 共用的需求算式與 purchase_decisions 表`。

---

### Task 2: 資料層：需求、決定、過期標示、KPI、報表

**Files:** Modify `dashboard.py`（`RecRow`、`_load_data`）、`main.py`（`_fetch_report_rows`、`_write_csv`、`_print_report`）；Test `tests/test_dashboard_decisions.py`（開發庫專用資料，仿 `tests/test_dashboard_review.py`）、`tests/test_dashboard_dedup.py`

**Interfaces:**
- `RecRow` 新增（皆有預設值，放最後）：`book_key: str | None = None`、`need: int = 0`（不設上限的需求冊數，`uncapped_copies`）、`decided: int | None = None`、`stale_from: int | None = None`（有決定且決定當時的系統建議≠現在的系統建議時，為當時的值）；屬性 `final -> int`：`decided` 不為 None 就用它，否則 `copies`。
- `_load_data`：每筆建議多讀 `Recommendation.book_key`、代表館藏的 `holdings_count`、關聯列的 `enrolled`（同課只算一次）→ 用 `demand.uncapped_copies` 算 `need`；一次載入全部 `purchase_decisions`；`kpis["copies_final"]` ＝ 本次顯示的建議的 `final` 加總。`kpis["copies"]`（系統建議）維持原樣。
- `main._fetch_report_rows`：每列新增 `need_copies`、`decided_copies`（可為 None）、`final_copies`；`_write_csv` 的欄位與標題、`_print_report` 同步；`/export/recommendations` 一併帶出。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_recrow_need_ignores_the_cap_and_suggestion_is_capped`（大班：需求 26、建議 5）。
  - `test_recrow_has_no_decision_by_default_and_final_is_the_suggestion`。
  - `test_a_stored_decision_overrides_the_suggestion_in_final`、`test_a_zero_decision_means_final_zero`。
  - `test_stale_marker_when_the_suggestion_changed_since_the_decision`、`test_no_stale_marker_when_the_suggestion_is_unchanged`。
  - `test_kpi_copies_final_uses_decisions_and_copies_stays_the_systems_number`。
  - `test_legacy_recommendation_without_a_book_key_has_no_decision_slot`（`book_key` 為 NULL → `book_key is None`、不會配到任何決定）。
  - `test_report_rows_carry_need_decided_and_final`（更新 `test_report_rows_join_course_names` 的鍵集合）、`test_export_csv_has_the_new_columns`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 採購建議帶出需求、人的決定與採用冊數`。

---

### Task 3: 決定端點

**Files:** Modify `dashboard.py`；Test `tests/test_purchase_endpoint.py`（`isolated_db`）

**Interfaces:**
- `POST /purchase/decide`：JSON `{book_key: str, copies: int | null}`。`copies` 為 `null` 或省略＝清除決定（刪除該列，不存在也回成功）；否則整數 0 到 9999（布林、浮點、字串都拒絕）。找不到該 `book_key` 的採購建議回 404。成功回 `{"ok": true, "copies": <現在的決定或 null>, "suggested": <系統建議>, "final": <最終冊數>}`。寫入時記 `suggested_at_decision`（當下的系統建議）與 `decided_at`。同一本再決定＝更新該列。驗證失敗回 400 且不寫入。只收 JSON。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_decide_stores_the_copies_and_the_suggestion_at_that_moment`、`test_deciding_again_updates_instead_of_duplicating`。
  - `test_clearing_removes_the_decision`（`copies: null` 與省略兩種）、`test_zero_is_stored_as_zero_not_cleared`。
  - `test_bad_copies_are_rejected_without_writing`（參數化：`-1`、`10000`、`1.5`、`"3"`、`True`、`[]`）、`test_missing_or_blank_book_key_is_rejected`、`test_unknown_book_key_is_404`、`test_decide_requires_json`。
  - `test_a_decision_survives_a_recommender_rerun`（`isolated_db` ＋ 假的 LLM／Alma，仿 `tests/test_recommender_node.py`：決定 → 重跑 `recommend()`（含建議被刪掉重建）→ `purchase_decisions` 仍在、`book_key` 仍對得上）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: POST /purchase/decide 記錄採購冊數的決定`。

---

### Task 4: 採購表的欄位、KPI、匯出與前端

**Files:** Modify `dashboard.py`（`_rec_row`、`_rec_table`、`_kpi_tiles`、CSS、JS）、`CLAUDE.md`

**Interfaces:**
- `_rec_table`／`_rec_row`：「冊數」格顯示系統建議；`data-need` 帶需求冊數，被上限截斷（`need > copies` 且有缺口）時加 `class="cut"`，由 CSS `::after` 畫「需求 N」（不放文字節點）。新增「決定冊數」欄（放在冊數之後）：`<input class="dec" type="number" min="0" max="9999" step="1" data-key="<逸出的 book_key>" placeholder="採用建議" value="<決定>">`；`book_key` 為 NULL 的列不給輸入框。有 `stale_from` 時加 `data-stale="<舊值>"`，由 CSS 畫「建議已從 A 變成 B」的提示。
- KPI：現有「建議採購總冊數」改名「系統建議冊數」，新增「採用冊數」卡片（`kpis["copies_final"]`）。
- JS：`input.dec` 的 `change` 事件 → `POST /purchase/decide`；成功更新該列（輸入框值、`data-final`、KPI「採用冊數」就地重算）；失敗還原並在該格顯示錯誤。匯出 CSV 改為：標題多「需求冊數／決定冊數／最終採購數」，每列的這三欄**明確**由 `data-need`、輸入框值、`final` 組出，其餘欄位仍讀 `textContent`。

- [ ] **Step 1: 寫失敗的測試**：
  - 渲染：`test_rec_row_shows_the_suggestion_the_need_marker_and_a_decision_box`、`test_cut_marker_only_when_the_need_exceeds_the_suggestion`、`test_decision_box_is_prefilled_with_the_stored_decision`、`test_legacy_row_has_no_decision_box`、`test_stale_decision_is_flagged`、`test_decision_cells_have_no_text_nodes`（標記與輸入框之間沒有文字）、`test_book_key_is_escaped_in_the_attribute`、`test_kpi_has_system_and_adopted_totals`。
  - jsdom（暫存區，不進版本庫）：輸入數字後觸發 `change` → 送出 `{book_key, copies}`；清空 → `copies: null`；伺服器回 400 → 還原並顯示錯誤；成功後 KPI「採用冊數」就地更新；匯出 CSV 含三個新欄且值正確（決定了的用決定、沒決定的最終＝系統建議、填 0 的最終為 0），且書名欄不含「需求」標記文字；採購表的篩選、搜尋、全選照常；審核分頁照常。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**（CSS 沿用現有變數；注意雙括號）。
- [ ] **Step 4: 執行確認通過**：jsdom 全過；`python -m pytest -q` 全過。
- [ ] **Step 5: 真實資料核對（唯讀）**：對開發庫產生整頁並以 jsdom 載入，確認沒有 JS 錯誤、被上限截斷的書（目前 85 本）都有「需求 N」標記、「系統建議冊數」與先前的總數一致；**不對開發庫做任何決定**。
- [ ] **Step 6: Commit**（`stage_mine.py`）：`feat: 採購表可填決定冊數，KPI 與匯出分系統建議與採用數`；並在 `CLAUDE.md` 記載決定冊數、`purchase_decisions`、`demand.py`、端點與限制（不記是誰決定、決定以 `book_key` 對應所以書的認定改變時會對不上）。
