# 審核動作（第三步）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在儀表板的審核區塊讓採購人員直接處理待審的書：配錯三選一、查無此書輸入書名加入或捨棄、可撤銷、可批次，並提供「套用審核結果」讓決定反映到採購清單。

**Architecture:** `verified_books` 加採購書名與決定欄位（不覆蓋驗證器配到的證據），下游讀 `buy_*` 欄位；新模組 `review_actions`（決定／撤銷）與 `review_apply`（重查館藏＋重算建議）；dashboard 加三個 POST 端點、審核卡片的按鈕與「已決定」分頁；`main review` 的核准／退回改走同一份邏輯。

**Tech Stack:** Python、SQLAlchemy 2（hybrid 屬性）、Alembic、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（`isolated_db`）、jsdom。

**Spec:** `docs/superpowers/specs/2026-10-31-review-panel-design.md`（第三步）

## Global Constraints

- migration 只加欄位（皆可為 NULL）、可 downgrade；現有資料不動。
- 採購書名**不得覆蓋** `canonical_*`；`buy_isbn` **不可用 coalesce**：有採購書名時一律用 `purchase_isbn`（可為 NULL），不沿用配到那本的 ISBN。
- 只用 JSON 的 POST 改資料；任何驗證失敗（空書名、超過 500 字、ISBN 格式錯、id 超過 200 個、未知 action）回 400 且**不寫入任何東西**。
- 只處理 `review_status='pending'` 的書目；已被處理的不覆蓋，回報跳過筆數。
- 套用必須與 `/run` 互斥（共用 `_run_lock` 與 `_run_state`）；失敗不標 `review_applied_at`。
- 書名、作者、理由是第三方文字：進 HTML 一律 `html.escape`；輸入框的值只在伺服器端驗證與落庫，不在前端拼 HTML。
- 匯出 CSV 讀採購表每格 `textContent`：審核區塊的輸入框與按鈕不在 `#rectable` 內，採購表不得新增文字節點。
- 在 `isolated_db` 以外的測試只能讀開發庫、播種專用資料並清掉；**任何會寫入決定的測試一律用 `isolated_db`，絕不對開發庫做真實決定**。
- HTML 是 f-string：JS/CSS 大括號寫成雙括號；**含反斜線的程式碼一律用 Write/Edit 工具寫入**（Bash heredoc 會把雙反斜線收成單個）；shell 的 `-c` 字串裡不要放 Windows 路徑。
- `git add` 一律列明確路徑；`dashboard.py`、`trace_view.py` 可能有使用者未提交的區塊，用 `scratchpad/stage_mine.py` 或先 `git diff` 確認；不提交 `nla.py`、`runlog.py`。
- 現有 255 個測試維持通過；因行為改變而必須改的測試要明確改並在 commit 說明。
- 對開發庫執行 `alembic upgrade head` 只在 Task 6，並先告知使用者。

## Review Focus

- 兩人同時處理同一組、或頁面過期：已被處理的不被覆蓋，回報跳過數。→ Task 2
- 撤銷已套用的 original／custom：用採購書名查的舊館藏不得留給之後的「買配對到的書」。→ Task 2
- 「買原書」時原書 ISBN 無效或空：`purchase_isbn` 為 NULL，不沿用配到那本的 ISBN。→ Task 1、2
- 自訂書名空白／超長、ISBN 格式錯：400，不寫入。→ Task 2、5
- 套用時 Alma 查詢失敗或 recommender 例外：不標已套用，下次可重試；不留半套。→ Task 4
- pipeline 正在跑時按套用被拒絕；決定本身（只寫資料庫）不受影響。→ Task 4
- 套用後重跑 validator／match_auditor：已決定的書不被洗掉。→ Task 3
- 人確認後「未驗證」標記消失；採購表與報表顯示採購書名。→ Task 3
- 書名含 `<script>` 或引號、極長：不得變成可執行的 HTML。→ Task 5

## File Structure

- Modify `src/library_agent/db/models.py`；Create `alembic/versions/<新>_add_review_decision_columns.py`；Test `tests/test_review_schema.py`
- Create `src/library_agent/review_actions.py`；Modify `src/library_agent/main.py`（`review` 的 a／r）；Test `tests/test_review_actions.py`
- Modify `src/library_agent/agents/librarian.py`、`agents/recommender.py`、`main.py`（報表標題）、`trace_view.py`（標題）、`dashboard.py`（建議標題與 `unverified`）；Test `tests/test_librarian.py`、`tests/test_recommender_node.py`、`tests/test_dashboard_review.py`、`tests/test_validator.py`
- Create `src/library_agent/review_apply.py`；Modify `dashboard.py`（`/review/apply`、背景執行緒）；Test `tests/test_review_apply.py`
- Modify `src/library_agent/review_groups.py`、`dashboard.py`（端點、已決定分頁、按鈕與橫幅）；Test `tests/test_dashboard_review.py`、`tests/test_review_groups.py`
- Modify `dashboard.py`（JS）、`CLAUDE.md`

---

### Task 1: 資料庫欄位與 `buy_*` 屬性

**Files:** Modify `db/models.py`；Create migration（`down_revision = "a7c4e1b9d3f2"`）；Test `tests/test_review_schema.py`（`isolated_db`）

**Interfaces:**
- `VerifiedBook` 新增欄位：`purchase_title: String(500) NULL`、`purchase_authors: Text NULL`（JSON 文字）、`purchase_isbn: String(20) NULL`、`review_choice: String(10) NULL`（`matched|original|custom|none`）、`reviewed_at: DateTime(timezone=True) NULL`、`review_applied_at: DateTime(timezone=True) NULL`。
- hybrid 屬性（Python 端與 SQL 運算式兩種都要能用）：`buy_title`（`purchase_title` 有值用它，否則 `canonical_title`）、`buy_authors`（同理，`canonical_authors`）、`buy_isbn`（`purchase_title` 有值時用 `purchase_isbn`，否則 `isbn_13`；用 `CASE`，不是 `coalesce`）。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_new_columns_exist_and_default_to_null`。
  - `test_buy_properties_fall_back_to_canonical_when_no_purchase_title`。
  - `test_buy_isbn_does_not_fall_back_to_the_matched_isbn_when_a_purchase_title_is_set`（採購書名有值、`purchase_isbn` 為 NULL → `buy_isbn is None`，即使 `isbn_13` 有值）。
  - `test_buy_expressions_work_in_sql`（用 `select(VerifiedBook.buy_title, VerifiedBook.buy_isbn)` 查回與 Python 端相同的值）。
- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_review_schema.py -q`。
- [ ] **Step 3: 實作** 欄位與 hybrid；寫 migration（upgrade 加六欄、downgrade 刪六欄）。
- [ ] **Step 4: 驗證 migration**：建一個用完即丟的資料庫（`library_agent_migtest`），對它跑 `alembic upgrade head` → `downgrade -1` → `upgrade head` 皆成功，欄位存在／消失；之後刪掉該資料庫。`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**：`git add src/library_agent/db/models.py alembic/versions/<新檔> tests/test_review_schema.py`，訊息 `feat: verified_books 加採購書名與審核決定欄位`。

---

### Task 2: 決定與撤銷（`review_actions`）

**Files:** Create `src/library_agent/review_actions.py`；Modify `main.py`（`review` 的 a／r）；Test `tests/test_review_actions.py`（`isolated_db`）

**Interfaces:**
- `ACTIONS = ("matched", "original", "custom", "none")`；`class ReviewError(ValueError)`。
- `@dataclass(frozen=True) DecideResult(changed: list[int], skipped: list[int])`、`UndoResult(changed: list[int], skipped: list[int])`。
- `decide(session, vb_ids: list[int], action: str, *, title: str | None = None, isbn: str | None = None, now: datetime | None = None) -> DecideResult`：語意見 spec 表。驗證：`action` 不在 `ACTIONS`、`vb_ids` 空或超過 200、`custom` 的書名去空白後為空或超過 500 字、`isbn` 有填卻通不過 `book_identity.normalize_isbn` → 丟 `ReviewError`（此時不寫任何東西）。只更新 `review_status == "pending"` 的列（`UPDATE … WHERE id IN … AND review_status = 'pending'`），其餘列入 `skipped`。寫入欄位：`review_status`（matched／original／custom → `approved`；none → `rejected`）、`review_choice`、`purchase_*`（original：原書名、原作者 JSON、`normalize_isbn(citation.isbn)`；custom：輸入書名、`[]`、正規化後的 ISBN；matched／none：全 NULL）、`reviewed_at=now`、`review_applied_at=NULL`。不 commit（由呼叫端負責）。
- `undo(session, vb_ids: list[int], *, now=None) -> UndoResult`：只處理 `review_choice IS NOT NULL` 的列；改回 `pending`，清空 `review_choice`、`purchase_*`、`reviewed_at`、`review_applied_at`；原選擇是 `original`／`custom` 者，**一併刪除該書的 `holding_checks` 與指向它們的 `recommendations`**；其餘列入 `skipped`。
- `main.review` 的 (a) 呼叫 `decide(..., "matched")`、(r) 呼叫 `decide(..., "none")` 後 commit；(e) 維持原行為。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_matched_approves_without_purchase_fields`、`test_original_copies_the_citation_title_authors_and_a_valid_isbn`、`test_original_with_an_invalid_citation_isbn_leaves_purchase_isbn_null`、`test_custom_stores_the_typed_title_and_normalized_isbn`、`test_none_rejects`。
  - `test_decide_skips_books_that_are_no_longer_pending_and_never_overwrites_them`。
  - `test_decide_rejects_bad_input_and_writes_nothing`（參數化：空書名、501 字書名、壞 ISBN、未知 action、空 id、201 個 id）。
  - `test_decide_applies_to_every_item_of_a_group`。
  - `test_undo_resets_the_book_to_pending_and_clears_purchase_fields`、`test_undo_of_original_or_custom_deletes_its_holding_and_recommendation`、`test_undo_of_matched_keeps_the_holding`、`test_undo_skips_books_without_a_decision`。
  - `test_cli_approve_and_reject_record_choice_and_time`（以 monkeypatch 假的 `input` 驅動 `main.review`）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作** `review_actions.py` 與 `main.review` 的調整。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**：`git add src/library_agent/review_actions.py src/library_agent/main.py tests/test_review_actions.py`，訊息 `feat: review_actions 記錄審核決定與撤銷，命令列審核改走同一份邏輯`。

---

### Task 3: 下游改讀採購書名

**Files:** Modify `agents/librarian.py`、`agents/recommender.py`、`main.py`、`trace_view.py`、`dashboard.py`；Test `tests/test_librarian.py`、`tests/test_recommender_node.py`、`tests/test_dashboard_review.py`、`tests/test_validator.py`

**Interfaces:**
- `librarian._check_one(vb)`：Alma 查詢用 `vb.buy_isbn`／`vb.buy_title`（其餘欄位維持）。
- 新增 `librarian.recheck_holdings(vb_ids: list[int]) -> RecheckResult(written: int, errors: list[str])`（`@dataclass(frozen=True)`）：對指定的書重查 Alma，沿用 `_process_one` 與 `_stage_save`（館藏有變就連帶刪掉指向舊館藏的建議），自己開 session 並 commit；個別失敗收進 `errors` 不中斷其他書。
- `recommender.recommender_node`：`title_by_holding` 與 `records`（同書認定的輸入）改用 `vb.buy_title`、`vb.buy_isbn`、`vb.buy_authors`。
- `dashboard._load_data`：建議的書名改選 `VerifiedBook.buy_title`；`RecRow.unverified` 改為「有書目 `verified=False` **且** `review_status != 'approved'`」（人確認後不再算未驗證）；`main._fetch_report_rows` 與 `trace_view.build_trace` 的書名同樣改用 `buy_title`。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_check_one_queries_alma_with_the_purchase_title_and_isbn`（monkeypatch `check_holding` 記錄參數）。
  - `test_check_one_uses_no_isbn_when_the_purchase_title_has_none`。
  - `test_recheck_holdings_rewrites_changed_holdings_and_reports_failures`（一本成功、一本丟例外 → `written == 1`、`errors` 一筆）。
  - recommender：`test_recommendation_uses_the_purchase_title_and_isbn_for_title_and_book_key`（兩個書目配到同一本的 ISBN，其中一個「買原書」→ 書名為原書名、不再與另一個合併）。
  - dashboard（開發庫專用資料）：`test_recrow_shows_the_purchase_title`、`test_unverified_marker_disappears_once_a_human_approved_the_book`。
  - validator：`test_validator_does_not_retry_books_with_a_human_decision`（`isolated_db` 播種 approved／rejected 的查無此書書目，monkeypatch `_validate_one` 為一呼叫就失敗 → 跑 `validator_node` 不會呼叫它，且列不被刪）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`dashboard.py` 用 `stage_mine.py`）：`feat: 館藏查詢、採購建議與報表改讀採購書名`。

---

### Task 4: 套用審核結果（`review_apply`）

**Files:** Create `src/library_agent/review_apply.py`；Modify `dashboard.py`（`POST /review/apply`、背景執行緒）；Test `tests/test_review_apply.py`（`isolated_db`）

**Interfaces:**
- `pending_apply_ids(session) -> list[int]`：`review_status='approved'` 且 `review_applied_at IS NULL` 的書目 id（`review_choice` 非空者）。
- `@dataclass(frozen=True) ApplyResult(applied: int, rechecked: int, errors: list[str])`。
- `apply_review() -> ApplyResult`：(1) 取 `pending_apply_ids`；(2) 其中「有 `purchase_title`、或沒有館藏列」者呼叫 `librarian.recheck_holdings`；(3) 呼叫 `recommender.recommender_node({})`；(4) 只有步驟 (2)(3) 都沒丟例外、且該書沒出現在 `recheck` 的錯誤裡，才標 `review_applied_at = now`。沒有待套用的書時什麼都不做、回 `ApplyResult(0, 0, [])`。
- `dashboard`：`POST /review/apply`——沒有待套用或已有工作在跑，回 `{"ok": False, "message": …}`；否則在背景執行緒跑 `apply_review`，共用 `_run_lock`／`_run_state`（`status` running → done／error）、`runlog.reset()/install()/uninstall()`，結束後頁面既有的輪詢會自動重整。

- [ ] **Step 1: 寫失敗的測試**（`isolated_db`；monkeypatch Alma 的 `check_holding`、recommender 的 `_llm_rationale`／`_make_judge`／`fetch_student_number`，仿 `tests/test_recommender_node.py`）：
  - `test_pending_apply_ids_lists_only_approved_unapplied_books`。
  - `test_apply_rechecks_overridden_books_then_recomputes_and_marks_applied`（買原書的書：用採購書名查館藏 → 出現在採購建議裡 → `review_applied_at` 有值）。
  - `test_apply_for_a_matched_book_with_a_holding_only_recomputes`（不重查 Alma）。
  - `test_apply_does_nothing_when_nothing_is_pending`。
  - `test_a_failed_recheck_leaves_the_book_unapplied_and_is_retried_next_time`。
  - `test_a_recommender_exception_marks_nothing_as_applied`。
  - `test_apply_endpoint_refuses_while_a_run_is_in_progress`、`test_apply_endpoint_says_nothing_to_apply`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 套用審核結果（重查館藏並重算採購建議）`。

---

### Task 5: 決定／撤銷端點與審核卡片的按鈕

**Files:** Modify `review_groups.py`、`dashboard.py`；Test `tests/test_dashboard_review.py`、`tests/test_review_groups.py`

**Interfaces:**
- `PendingItem` 新增（皆有預設值）：`choice: str | None = None`、`buy_title: str | None = None`、`applied: bool = False`；`ReviewGroup` 同名三欄。`group_pending` 照舊分組並帶出（同組成員取第一筆的值；`applied` 為全部成員皆已套用）。
- `dashboard._load_data` 回傳新增：`"decided"`（`review_choice` 非空者以 `group_pending` 分組後攤平成一個清單，依決定時間新到舊）、`"pending_apply"`（待套用的書目數）。待審分組仍只含 `review_status='pending'`。
- `POST /review/decide` 與 `POST /review/undo`：解析 JSON → 呼叫 `review_actions` → commit；`ReviewError` 回 400（內容為訊息）；成功回 `{"ok": True, "changed": n, "skipped": [ids]}`。
- `_review_panel`：每組一列加動作區——`mismatch`：三顆按鈕（買原書／買配對到的書／都不買）；`notfound`：書名輸入框（預填原書名）、ISBN 輸入框（選填）、「加入採購清單」、「捨棄」；`other`：買這本書／都不買。列上有 `data-ids`、動作按鈕有 `data-act`。新增第四個分頁「已決定」（`data-k="decided"`）：每組顯示決定、採購書名、狀態（待套用／已套用）、「撤銷」。卡片頂端橫幅：「N 筆決定待套用」與「套用並重算採購建議」按鈕（`id="applybtn"`，N 為 0 時不顯示）。每個待審列有核取方塊（`class="rchk"`）與卡片頂端的批次列（「買配對到的書」「都不買」兩顆）。

- [ ] **Step 1: 寫失敗的測試**：
  - 端點（`isolated_db` ＋ monkeypatch `dashboard.SessionLocal`）：`test_decide_endpoint_applies_to_all_ids_and_reports_skipped`、`test_decide_endpoint_400_on_bad_input_and_writes_nothing`、`test_undo_endpoint_restores_pending`、`test_decide_endpoint_requires_json`（非 JSON 內容回 4xx）。
  - 渲染：`test_mismatch_row_has_three_action_buttons`、`test_notfound_row_has_a_prefilled_title_box_an_isbn_box_and_two_buttons`、`test_other_row_has_two_buttons`、`test_decided_tab_lists_choice_status_and_undo`、`test_apply_banner_shows_only_when_there_is_something_to_apply`、`test_action_area_escapes_untrusted_text`（書名含 `<script>` 與引號，輸入框預填值也要逸出）、`test_batch_bar_offers_only_matched_and_none`。
  - 資料：`test_load_data_puts_decided_books_in_decided_not_in_pending_groups`、`test_pending_apply_counts_approved_unapplied_books`。
  - `test_group_pending_carries_choice_buy_title_and_applied`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**（CSS 沿用現有變數；輸入框與按鈕不得放進 `#rectable`）。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 審核卡片的決定、撤銷與「已決定」分頁`。

---

### Task 6: 前端互動、驗證與文件

**Files:** Modify `dashboard.py`（JS）、`CLAUDE.md`

**Interfaces:**
- 單組動作：點 `[data-act]` → 以該列 `data-ids`（逗號分隔轉成整數陣列）與動作（`custom` 帶書名與 ISBN 輸入框的值）`POST /review/decide`；成功則該列淡出並更新分頁標籤的組數與橫幅的待套用數；`ok: false` 或 400 在該列顯示錯誤訊息，列保留。
- 批次：勾選多列後點批次鈕 → `confirm("確定對 N 組（M 筆書目）執行「…」？")`，確認才送出（所有勾選列的 id 合併成一個請求）。
- 撤銷：點「撤銷」→ `POST /review/undo`。
- 套用：點 `#applybtn` → `POST /review/apply`；沿用既有的狀態輪詢（執行中停用按鈕與「執行 pipeline」，結束後頁面自動重整）。
- 採購表的篩選、搜尋、全選、匯出完全不變。

- [ ] **Step 1: 寫 jsdom 驗證腳本（暫存區，不進版本庫）並看它失敗**：假的 `fetch` 記錄請求；驗證——三種列的按鈕送出正確的 `ids`／`action`／（custom 的）`title`／`isbn`；成功後該列移走並更新計數；400 時顯示錯誤、列保留；批次先 `confirm`、取消則不送；撤銷送出正確 ids；套用按鈕送 `/review/apply`；輸入框值含引號與 `<script>` 時請求內容正確且頁面沒有被注入；匯出 CSV 不含審核區塊的任何文字；採購表篩選／搜尋照常。
- [ ] **Step 2: 實作** JS（注意雙括號）。
- [ ] **Step 3: 執行確認通過**：jsdom 腳本全過；`python -m pytest -q` 全過。
- [ ] **Step 4: 開發庫與真實資料核對**：先告知使用者，再對開發庫執行 `alembic upgrade head`（只加欄位）；用真實資料產生整頁並以 jsdom 載入，確認沒有 JS 錯誤、四個分頁與橫幅正確、待審分組數與先前一致（537／426／2 組）。**不對開發庫做任何真實決定。**
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 審核按鈕的前端互動`；並在 `CLAUDE.md` 記載審核動作、`buy_*` 欄位、套用流程、三個端點與已知限制（沒有登入、買原書的館藏可能混到同名他書）。
