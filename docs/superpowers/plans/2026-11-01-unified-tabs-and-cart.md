# 分頁操作統一與採購單（購物車）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) 每個分頁（採購建議、採購單、配錯、查無此書、其他、審核紀錄）用同一套「找、選、處理」的操作模式：共用工具列（搜尋、篩選、匯出）、一致的欄位骨架、一致的批次；(2) 新增「採購單」（購物車）：**明確加入才算要買**，採購單的匯出就是最後要下單的清單。

**Architecture:** 採購單不是另一張表，而是採購建議表的「檢視」（`data-incart` 的列）：決定冊數就地更新時，採購單的內容與計數自然同步，不會過期。新端點 `POST /purchase/decide-many` 處理採購建議／採購單的批次。搜尋、篩選、全選、匯出改成對「目前開著的分頁的表」生效。審核紀錄（原「已決定」）的追溯端點改為也接受已決定的書。

**Tech Stack:** Python、SQLAlchemy 2、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（`isolated_db`）、jsdom。

**Spec:** `docs/superpowers/specs/2026-10-31-review-panel-design.md`（「第四步：分頁操作統一與採購單」）

## 語意變更（請特別注意，會改變第二步剛做好的行為）

`purchase_decisions` 的一列從「決定」變成「採購單的內容」：

| 狀態 | 資料 | 在採購單裡？ | 最終冊數 `final` |
|---|---|---|---|
| 沒決定（沒有列） | — | **否** | 0 |
| 決定冊數 N（N ≥ 1） | `copies = N` | **是** | N |
| 這次不買 | `copies = 0` | 否（明確排除） | 0 |

- **舊規則「留空＝採用系統建議」取消**，改為「留空＝還沒決定、不在採購單」。系統建議永遠只是參考。
- `final = decided or 0`（原本沒決定時是系統建議）。KPI「採用冊數」改名「**採購單冊數**」並新增「採購單書數」；以真實資料現況（2 筆決定）會從 2878 降到 17，這是預期的。
- 匯出 CSV 的「最終採購數」：沒決定的書為 0。`library-agent report` 的「合計採用冊數」改為「合計採購單冊數」。
- 「加入採購單」＝把決定冊數設為**目前的系統建議**（系統建議為 0 的書不能這樣加，需自己填冊數）。

## Global Constraints

- 不改資料庫結構（沿用 `purchase_decisions`，語意如上表）；現有 2 筆決定自動成為採購單內容。
- 決定一律以 `book_key` 對應；`book_key` 為 NULL 的舊格式建議不能加入採購單（沒有按鈕與輸入框）。
- 批次一次最多 200 本；前端自動分批。驗證失敗回 400 且**不寫入任何東西**。只收 JSON。
- 「加入採購單」用**伺服器端**當下的系統建議（`mode="suggested"`），不用頁面載入時的舊值。
- 匯出 CSV 讀每格 `textContent`：輸入框的值、「需求」「建議已從 A 變成 B」、按鈕都不是匯出內容；匯出程式依各分頁的欄位規格**明確**組欄位。
- 搜尋用列上的 `data-search`（伺服器依各表的內容組好、已逸出），不再依欄位位置取值。維持既有行為：搜尋框有字時只看搜尋（無視優先級／類別按鈕）。
- 切換分頁時重設搜尋與篩選；工具列依分頁顯示適用的篩選（`data-filters`）。
- 展開鈕的事件只在卡片（`#review`）綁**一次**（綁兩次會開了又關，已出過事）；jsdom 驗證要檢查「看得見」，不只是「有建立」。
- HTML 是 f-string：JS/CSS 大括號寫成雙括號；**含反斜線的程式碼一律用 Write/Edit 工具寫入**（Bash heredoc 會把雙反斜線收成單個）；shell 的 `-c` 字串裡不要放 Windows 路徑。
- 寫入決定的測試一律用 `isolated_db`；開發庫的真實資料只做**唯讀**核對，**不做任何決定**。真實資料庫裡已有使用者自己填的決定，測試不可假設它是空的。
- `git add` 一律列明確路徑；`dashboard.py` 可能有使用者未提交的區塊，用 `scratchpad/stage_mine.py`；不提交 `nla.py`、`runlog.py`。
- 現有 495 個測試維持通過；因語意變更而必須改的測試要明確改並在 commit 說明（列在各 task）。

## Review Focus

- 沒決定的書不在採購單、最終冊數為 0；0 與清除不同（0＝明確排除、清除＝回到沒決定）。→ Task 1、4
- 「加入採購單」對系統建議為 0 的書：不加、回報略過幾本；批次不因此整批失敗。→ Task 2
- 批次中有不存在的 `book_key`、非法 mode、空清單、超過 200：400／略過，不寫入或不誤寫。→ Task 2
- 已決定的書（審核紀錄）展開追溯：看得到「人工決定：…」而不是「未進入採購建議」。→ Task 3
- 切換分頁後搜尋框、篩選按鈕、批次勾選不殘留；配錯分頁沒有優先級篩選。→ Task 5
- 在採購建議改冊數／加入／移出：採購單分頁的內容、頁籤上的「N 本·M 冊」與 KPI 同步，不用重新整理。→ Task 5
- 審核決定後重新整理仍回到原本的捲動位置與分頁。→ Task 5
- 匯出各分頁的欄位正確，採購單匯出只含採購單內的書、冊數為決定冊數。→ Task 5

## File Structure

- Modify `dashboard.py`（`RecRow`、`_load_data`、KPI、端點、`_rec_row`／`_rec_table`、`_review_*`、`_review_panel`、CSS、JS）、`main.py`（報表）、`trace_view.py`（已決定的追溯）
- Test `tests/test_dashboard_decisions.py`、`tests/test_dashboard_decision_ui.py`、`tests/test_purchase_endpoint.py`、`tests/test_dashboard_review.py`、`tests/test_trace_view.py`、新檔 `tests/test_purchase_batch.py`、`tests/test_dashboard_tabs.py`

---

### Task 1: 採購單語意（資料層、KPI、報表）

**Files:** Modify `dashboard.py`（`RecRow`、`_load_data`、`_kpi_tiles`）、`main.py`；Test `tests/test_dashboard_decisions.py`、`tests/test_dashboard_decision_ui.py`

**Interfaces:**
- `RecRow.final`：`decided` 不為 None 就用它，否則 **0**。新增屬性 `RecRow.in_cart -> bool`（`decided` 不為 None 且大於 0）。
- `_load_data` 的 `kpis`：`copies_final`（採購單冊數＝各列 `final` 加總）、新增 `cart_books`（採購單書數＝`in_cart` 的列數）；`copies`（系統建議）不變。
- `_kpi_tiles`：「採用冊數」改名「採購單冊數」（id `kpi-final`）、新增「採購單書數」（id `kpi-cart-books`）。
- `main._fetch_report_rows` 的 `final_copies`＝決定冊數或 0；`_print_report` 的「合計採用冊數」改為「合計採購單冊數」。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_final_is_zero_when_nothing_was_decided`、`test_in_cart_only_when_the_decision_is_above_zero`（無決定／0／1 以上）、`test_kpi_cart_totals_count_only_decided_books`（`copies_final` 與 `cart_books`；`copies` 不受影響）。
  - 報表：`test_report_final_is_zero_for_undecided_books`。匯出：`test_export_csv_final_column_is_zero_for_undecided`。
  - 因語意變更而**必須修改**的既有測試（在 commit 說明）：`test_recrow_has_no_decision_by_default_and_final_is_the_suggestion`（final 改為 0）、`test_a_stored_decision_overrides_the_suggestion_in_final`、`test_kpi_copies_final_uses_decisions_and_copies_stays_the_systems_number`（差額改為只加決定的冊數）、`test_adopted_total_is_the_sum_of_the_final_copies_of_the_listed_recommendations`、`test_report_rows_carry_need_decided_and_final`、`test_export_csv_has_the_new_columns`、`test_row_carries_the_final_copies_for_the_kpi_and_export`、`test_kpi_has_system_and_adopted_totals`（標題與 id）、`test_kpi_adopted_falls_back_to_the_systems_number_when_missing`（改為缺鍵時為 0）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 採購單語意：沒決定的書不在採購單，最終冊數為 0`。

---

### Task 2: 批次決定端點

**Files:** Modify `dashboard.py`；Test `tests/test_purchase_batch.py`（`isolated_db`）

**Interfaces:**
- `POST /purchase/decide-many`：JSON `{book_keys: [str, ...], mode: "suggested" | "zero" | "clear"}`。
  - `suggested`：把每本的決定冊數設為**伺服器端當下的系統建議**（記 `suggested_at_decision`）；系統建議為 0 的略過。
  - `zero`：決定冊數設為 0（明確排除，記當下的系統建議）。
  - `clear`：刪除決定（不要求建議還在）。
  - 回 `{"ok": true, "changed": n, "skipped": [book_key...], "results": {book_key: {"copies": int | null, "final": int}}}`；`skipped` 含找不到建議的 key（`clear` 除外）與 `suggested` 時建議為 0 的 key。
  - 400（不寫入任何東西）：`book_keys` 不是非空的字串清單、超過 200 個、含空白字串、`mode` 不在三者之內。

- [ ] **Step 1: 寫失敗的測試**：`test_suggested_sets_each_book_to_its_current_suggestion`（用伺服器當下的值，不是客戶端送的）、`test_suggested_skips_books_whose_suggestion_is_zero_and_reports_them`、`test_zero_marks_books_as_explicitly_excluded`、`test_clear_removes_decisions_even_when_the_recommendation_is_gone`、`test_unknown_keys_are_skipped_not_fatal`、`test_results_carry_the_new_copies_and_final_for_each_key`、`test_bad_requests_are_rejected_without_writing`（參數化：空清單、201 個、非清單、含空字串、含非字串、未知 mode）、`test_decide_many_requires_json`、`test_batch_is_atomic_when_validation_fails`、`test_a_decision_made_in_batch_survives_a_recommender_rerun`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**（沿用 `purchase_decide` 的 upsert；全部在一個交易裡）。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: POST /purchase/decide-many 批次加入／排除／清除採購單`。

---

### Task 3: 審核紀錄也能展開看決策過程

**Files:** Modify `trace_view.py`、`dashboard.py`（端點說明）；Test `tests/test_trace_view.py`

**Interfaces:**
- `trace_view.build_pending_trace(session, vb_ids)`：除了待審（`review_status == "pending"`）也接受**有決定紀錄**（`review_choice` 非空）的書目。
- `Trace` 新增 `decision: str | None = None`：對已決定的書，內容如「人工決定：買原書（採購書名「X」）；待套用」。有 `decision` 時第 ⑤ 段顯示它，**不再顯示**「未進入採購建議」；`pending_reason` 對已決定的書為 `None`。一組裡混有待審與已決定時，各自照自己的狀態處理（`decision` 取已決定者，以「；」串接去重）。
- `GET /trace/pending?ids=…`：端點名稱不變；全部既非待審也沒有決定紀錄時回 404。

- [ ] **Step 1: 寫失敗的測試**：`test_decided_books_can_be_traced`（matched／original／custom／none 各一）、`test_the_section_five_of_a_decided_book_shows_the_decision_not_the_pending_note`（含「待套用」「已套用」）、`test_decision_text_escapes_the_purchase_title`、`test_a_book_that_is_neither_pending_nor_decided_is_still_none`、`test_endpoint_accepts_decided_ids`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 審核紀錄的書也能展開看決策過程`。

---

### Task 4: 統一的版面（渲染）

**Files:** Modify `dashboard.py`（`_rec_row`、`_rec_table`、`_review_row`、`_decided_row`、`_review_panel`、CSS）；Test `tests/test_dashboard_tabs.py`（新）、`tests/test_dashboard_decision_ui.py`、`tests/test_dashboard_review.py`

**Interfaces:**
- 分頁列（順序固定）：`採購建議 N 本｜採購單 N 本·M 冊｜配錯｜查無此書｜其他｜審核紀錄`（沒有審核項目與紀錄時只有前兩個）。「採購單」分頁按鈕 `data-k="cart" data-panel="recs"`：與採購建議**共用同一個面板與同一張表**，只是顯示 `data-incart="1"` 的列。「已決定」改名「審核紀錄」（內部 `data-k="decided"` 不變）。
- **共用工具列**（分頁列下方、面板上方，`id="toolbar"`）：搜尋框 `#recsearch`、優先級按鈕組（`.tb-prio`）、類別按鈕組（`.tb-cat`）、匯出按鈕與面板（沿用現有）。每個面板有 `data-filters="prio cat"`（採購建議、採購單）、`"prio"`（查無此書、其他）、`""`（配錯、審核紀錄）決定工具列顯示哪些篩選；`data-export="recs|cart|review|log"`。
- **欄位骨架**：每張表第一格是勾選，其次依序為「優先級｜課程｜▸ 書名｜…」，最右一欄是「處理」。
  - 採購建議／採購單：勾選｜優先級｜課程｜▸書名｜類別｜館藏狀態｜冊數（含「（需求 N）」）｜理由｜**處理**（決定冊數輸入框＋「加入採購單／移出」按鈕＋過期提示）。**採購表原有欄位順序不變，只把「決定冊數」搬到最右並加按鈕**。
  - 審核三分頁：勾選｜優先級（原「採購清單」欄併入：優先級膠囊＋冊數，或「已擋下」「不在採購清單」）｜課程（含各課人數）｜▸書名（含作者）｜配到的書｜原因｜人數合計｜**處理**。
  - 審核紀錄：勾選｜課程｜▸書名｜決定｜採購書名｜狀態｜**處理**（撤銷）。
- 列上資料：每列有 `data-search`（小寫、已逸出：課程、書名、配到的書等可搜的文字）；採購列另有 `data-incart="1"`（在採購單時）；審核列有 `data-priority`（優先級或 `none`）。
- 每個面板頂端有同一個批次列 `.rbatch`：採購建議／採購單用 `data-pbatch="suggested|zero|clear"`（採購建議：加入採購單（採用系統建議）／設為不買／清除決定；採購單：移出採購單／設為不買）；審核三分頁維持原有 `data-batch`；審核紀錄 `data-ubatch="undo"`（撤銷所選）。
- 「加入採購單」按鈕：`data-cart`，系統建議為 0 且不在採購單時 `disabled`（提示「系統建議為 0，請直接填冊數」）。
- 拿掉分頁列右側的「匯出待審清單 CSV」連結（匯出由共用工具列負責）；`/export/pending` 端點保留。
- KPI：沿用 Task 1。

- [ ] **Step 1: 寫失敗的測試**：
  - 分頁：`test_tab_bar_order_and_cart_tab_label`（含 `data-panel="recs"`）、`test_review_tab_is_renamed_to_review_log`、`test_cart_tab_label_counts_books_and_copies`、`test_no_review_tabs_when_there_is_nothing_to_review`。
  - 工具列：`test_one_toolbar_with_search_filters_and_export`、`test_each_panel_declares_which_filters_apply`（配錯與審核紀錄為空、查無此書只有 prio、採購建議有 prio 與 cat）。
  - 欄位：`test_purchase_table_keeps_its_column_order_with_the_decision_at_the_far_right`、`test_review_tables_start_with_checkbox_priority_course_title_and_end_with_actions`、`test_review_log_has_checkbox_expand_and_a_course_column`。
  - 列資料：`test_rows_carry_data_search_escaped`、`test_cart_rows_are_marked_incart_only_when_the_decision_is_above_zero`、`test_review_rows_carry_data_priority`。
  - 按鈕：`test_row_button_says_add_or_remove_by_state`、`test_add_button_is_disabled_when_the_suggestion_is_zero`、`test_every_panel_has_a_batch_bar_with_the_right_buttons`（含批次列沒有「買原書」「輸入書名」）。
  - 逸出：`test_untrusted_text_is_escaped_everywhere_in_the_new_cells`。
  - 因版面變更而**必須修改**的既有測試（在 commit 說明）：`test_review_panel_*` 中依「已決定」文字、欄位數、`採購清單` 欄的斷言；`test_decided_tab_lists_choice_status_and_undo`（改名、新欄位、不再沒有 ▸）；`test_the_header_has_a_decision_column_after_the_copies_column`（決定欄移到理由之後）；`test_rec_row_shows_the_suggestion_the_need_marker_and_a_decision_box` 等（儲存格順序）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 各分頁統一的版面與採購單分頁`。

---

### Task 5: 前端：共用工具列、採購單、批次、捲動位置

**Files:** Modify `dashboard.py`（JS）、`CLAUDE.md`；jsdom 驗證腳本放暫存區（不進版本庫）

**Interfaces:**
- `_activePanelTable()`：目前開著的分頁的 `table`（採購單檢視時是採購建議那張表）。搜尋、篩選、全選、匯出一律對它生效；`_applyRecFilters()` 改為：列的顯示＝（採購單檢視時須 `data-incart="1"`）且（搜尋框有字：`data-search` 含關鍵字；否則：優先級與類別按鈕）。維持「搜尋框有字時無視按鈕」的既有行為。
- 切換分頁（含「採購單」）：重設搜尋與按鈕、依面板的 `data-filters` 顯示／隱藏 `.tb-prio`、`.tb-cat`、依 `data-export` 調整匯出面板（優先級勾選項只在有優先級的分頁顯示）、再套用篩選。
- 採購單檢視：頁籤標籤、KPI（`#kpi-final`、`#kpi-cart-books`）由 DOM 即時計算（各列 `data-final`、`data-incart`），在改冊數、加入、移出、批次後就地更新，並重新套用篩選（移出的列隨即從採購單檢視消失）。
- 單列：`[data-cart]` 按鈕＝加入（`POST /purchase/decide-many`，`mode="suggested"`）或移出（`mode="clear"`）；輸入框 `change` 沿用 `POST /purchase/decide`，並同步 `data-incart`／按鈕文字／標籤。
- 批次：`[data-pbatch]` → 確認視窗（寫明筆數與動作）→ 依 200 一批呼叫 `/purchase/decide-many` → 依 `results` 就地更新各列；`[data-ubatch="undo"]` → 沿用 `/review/undo`（分批）後整頁重新整理。
- 匯出（依 `data-export`）：`recs` 欄位同現在（含需求／決定／最終，最終＝決定或 0）；`cart` 同欄位但只含採購單內的列；`review`：優先級、課程、書名、配到的書、原因、人數合計；`log`：課程、書名、決定、採購書名、狀態。「只匯出勾選的」所有分頁通用。檔名依分頁（`recommendations_`、`cart_`、`review_<分頁>_`、`log_`）。
- 捲動位置：審核決定、撤銷等需要整頁重新整理的動作，重新整理前把 `scrollY` 與目前分頁存進 `sessionStorage`，載入後還原；有存捲動位置時不再設 `#review`（會跳回卡片頂端）。

- [ ] **Step 1: 寫 jsdom 驗證腳本並看它失敗**（以合成頁面；`fetch`、`confirm`、`alert`、`Blob` 皆假的；記錄請求）：
  - 工具列：切換到每個分頁，確認 `.tb-prio`／`.tb-cat` 的顯示符合 `data-filters`、搜尋與按鈕被重設；審核分頁能用搜尋框篩出書；配錯分頁沒有優先級按鈕。
  - 採購單：在採購建議填冊數 → 採購單分頁立即出現該列、頁籤與 KPI 即時更新；清空 → 該列消失；「加入採購單」送出 `mode="suggested"`、按鈕變「移出」；系統建議為 0 的列按鈕停用。
  - 批次：沒勾選時提示；取消確認不送出；確認後依 200 一批送出（造 205 本驗證 200＋5）；`results` 就地更新各列；採購單批次「移出」讓列消失。
  - 展開：每個分頁的 ▸ **點一次就看得見**、再點收合；審核紀錄的 ▸ 取 `/trace/pending?ids=…`。
  - 匯出：每個分頁的標題與欄位正確；採購單只含 `data-incart` 的列；最終採購數＝決定冊數；不含「（需求」「建議已從」等標記文字；勾選匯出通用。
  - 捲動位置：審核決定後 `sessionStorage` 有 `scrollY` 與分頁，且沒有設 `#review`；還原後清掉暫存。
  - 既有腳本（`reviewcheck`、`actioncheck`、`decisioncheck`）依新版面更新後全過。
- [ ] **Step 2: 實作** JS（注意雙括號）。
- [ ] **Step 3: 執行確認通過**：jsdom 全過；`python -m pytest -q` 全過。
- [ ] **Step 4: 真實資料核對（唯讀）**：對開發庫產生整頁並以 jsdom 載入，確認沒有 JS 錯誤；採購單分頁只有使用者已有的決定（目前 2 本）、頁籤與 KPI 一致；六個分頁的列數與先前一致；**不做任何決定**。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 分頁共用工具列、採購單與一致的批次操作`；並在 `CLAUDE.md` 更新：採購單語意（沒決定＝不在採購單、0＝明確排除、`final` 的定義）、共用工具列與各分頁的欄位骨架、`/purchase/decide-many`、審核紀錄可追溯、捲動位置還原。
