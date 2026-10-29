# 跨課程去重與合計 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同一本書不論被幾門課用到，只產生一筆採購建議；冊數依所有課的人數合計算、現有館藏只扣一次。

**Architecture:** 新增純函式模組 `book_identity` 決定「同一本書」的識別鍵（有效 ISBN，否則書名＋重用 match_auditor 的判斷）；`recommender` 依鍵分組、合併計算（`_decide` 不改）、寫成一本書一列，另以新表 `recommendation_courses` 記錄用到的課；dashboard、報表、匯出改讀新結構。

**Tech Stack:** Python、SQLAlchemy + Alembic（PostgreSQL）、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（真實 DB 播種）。

**Spec:** `docs/superpowers/specs/2026-10-29-dashboard-user-feedback-design.md`（第 4 點）

## Global Constraints

- migration 只加不改、可 `downgrade`，且**向下相容**：舊程式在新結構上仍能運作（新欄位可為 NULL）。
- `recommendation_courses` 的 `recommendation_id` 與 `citation_id` 外鍵都是 `ON DELETE CASCADE`。
- `recommender._decide` 與 `_resolve_enrolled` 不改（既有測試依賴它們）。
- 仍以 `db/filters.py` 的 `purchasable()` 與 `not_superseded()` 篩可採購的書。
- 不同的有效 ISBN 不合併；判斷（LLM）失敗時**不合併**（與 match_auditor 稽核時預設相反）。
- 同一門課重複列同一本書，人數只算一次；任一課指定就按指定算。
- 上限沿用 `max_purchase_copies`（設定值，不改數字）。
- HTML 是 f-string，JS/CSS 大括號寫成雙括號；**含反斜線的程式碼一律用 Write/Edit 工具寫入**（Bash heredoc 會把雙反斜線收成單個），寫完用 `od` 或測試確認。
- `git add` 一律列明確路徑；工作區有使用者未提交的檔案（含 `dashboard.py` 第 706 行行尾空白、`runlog.py` 註解、`nla.py` 等），不得夾帶進 commit。
- 現有 130 個測試維持通過。

## Review Focus

- 兩本作者不同、共用同一個**無效 ISBN**（Alma 記錄編號）的書：不得被合併。→ Task 1
- 判斷函式拋例外或 LLM 失敗：不合併，而不是整個 recommender 崩潰或誤併。→ Task 1
- 有有效 ISBN 的書與沒有 ISBN 的同名書：不合併。→ Task 1
- 以 `course_ids` 限縮的執行（只跑某學系）：合計仍須是全校，不能只算選中的課，也不能刪掉別的課的建議。→ Task 4
- 同一門課重複列同一本：人數只算一次。→ Task 3、4
- 重跑沒有變動：不得再呼叫 LLM，建議的 id 不變。→ Task 4
- validator / librarian 刪除某筆建議後：關聯表的列要跟著消失，不得被外鍵擋住。→ Task 2
- 學系篩選：書有任一門課屬於所選學系就顯示，冊數是全校合計。→ Task 5

## File Structure

- Create `src/library_agent/book_identity.py`：識別鍵（純函式 + 判斷器工廠）。
- Modify `src/library_agent/agents/match_auditor.py`：`_llm_same_book` 加 `fail_as_same` 參數。
- Modify `src/library_agent/db/models.py`、Create `alembic/versions/a7c4e1b9d3f2_add_book_key_and_recommendation_courses.py`
- Modify `src/library_agent/agents/recommender.py`、`src/library_agent/prompts/recommender_prompts.py`
- Modify `src/library_agent/dashboard.py`、`src/library_agent/main.py`
- Create `tests/test_book_identity.py`、`tests/test_recommendation_schema.py`、`tests/test_recommender_merge.py`、`tests/test_recommender_node.py`；Modify `tests/test_dashboard_filters.py`

---

### Task 1: 書的識別鍵

**Files:** Create `src/library_agent/book_identity.py`；Modify `src/library_agent/agents/match_auditor.py`；Test `tests/test_book_identity.py`

**Interfaces:**
- `match_auditor._llm_same_book(client, model, orig_title, orig_authors, matched_title, matched_authors, fail_as_same: bool = True) -> tuple[bool, str]`：`fail_as_same=False` 時，三次重試都失敗回 `(False, 原因)`；預設行為不變。
- `book_identity.normalize_isbn(raw: str | None) -> str | None`：去掉連字號與空白；過檢查碼的 ISBN-13 回 13 碼；過檢查碼的 ISBN-10 轉成 13 碼；其他（含空）回 `None`。
- `book_identity.assign_book_keys(records: list[dict], judge: Callable[[dict, dict], bool]) -> dict[int, str]`：`records` 每筆含 `id`、`isbn`、`title`、`authors: list[str]`；回傳 `id → book_key`。依 `id` 排序處理以求確定性。
- `book_identity.llm_judge(client, model) -> Callable[[dict, dict], bool]`：以 `_llm_same_book(..., fail_as_same=False)` 實作。

規則：有效 ISBN → `isbn:<13碼>`；否則以 `match_auditor._norm(title)` 分桶，桶內第一群 `title:<正規化書名>`，其後 `#2`、`#3`…；桶內與群代表「書名正規化與作者集合都相同」者免呼叫判斷，否則呼叫 `judge(代表, 本筆)`；有有效 ISBN 的記錄永遠不進書名桶；正規化書名為空者鍵為 `rec:<id>`；書名部分截到 450 字元。

- [ ] **Step 1: 寫失敗的測試**（`tests/test_book_identity.py`）：
  - `test_normalize_isbn`：`"978-0-306-40615-7"`→`"9780306406157"`；`"0306406152"`→`"9780306406157"`；檢查碼錯 `"9780306406158"`→`None`；Alma 編號 `"991010871719705721"`→`None`；`None` 與 `""`→`None`。
  - `test_isbn10_and_isbn13_of_the_same_book_share_a_key`
  - `test_invalid_isbn_falls_back_to_title_and_judge`：兩筆共用 `991010871719705721`、書名同、作者不同，`judge` 回 False → 兩個不同的鍵（這就是會誤併《經濟學》的案例）。
  - `test_identical_records_skip_the_judge`：`judge` 一被呼叫就 `AssertionError`，兩筆書名、作者相同的無 ISBN 記錄得到同一個鍵。
  - `test_title_clusters_get_numbered_keys`：三筆同書名，作者 A、B、A（judge 以作者是否相同判斷）→ 鍵為 `title:x`、`title:x#2`、`title:x`。
  - `test_isbn_book_never_merges_with_no_isbn_book_of_same_title`
  - `test_keys_are_deterministic_regardless_of_input_order`
  - `test_llm_judge_failure_means_not_merged`：fake client 的 `create` 一律丟例外 → `llm_judge(...)` 回 False。
  - `test_llm_same_book_default_still_fails_as_same`（`tests/test_match_auditor.py` 旁新增或同檔）：不帶新參數時，失敗回 `(True, ...)`。

- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_book_identity.py -q`，預期 ImportError。
- [ ] **Step 3: 實作** 上述介面。
- [ ] **Step 4: 執行確認通過**：同上，加跑 `python -m pytest -q` 全部通過。
- [ ] **Step 5: Commit**：`git add src/library_agent/book_identity.py src/library_agent/agents/match_auditor.py tests/test_book_identity.py`，訊息 `feat: book_identity 識別同一本書（有效 ISBN，否則書名＋重用 match_auditor 判斷）`。

---

### Task 2: 資料庫結構

**Files:** Modify `src/library_agent/db/models.py`；Create `alembic/versions/a7c4e1b9d3f2_add_book_key_and_recommendation_courses.py`（`down_revision = 'e3f6a2c9d8b1'`）；Test `tests/test_recommendation_schema.py`

**Interfaces:**
- `Recommendation.book_key: Mapped[str | None] = mapped_column(String(500), unique=True)`。
- `class RecommendationCourse(Base)`，`__tablename__ = "recommendation_courses"`，欄位與外鍵見設計文件；`UniqueConstraint("recommendation_id", "citation_id")`。
- migration `upgrade`：加欄位、建唯一索引、建新表；`downgrade` 反向。

- [ ] **Step 1: 寫失敗的測試**（真實 DB 播種專用資料、`finally` 清理；測試前先 `alembic upgrade head`）：
  - `test_book_key_is_unique_but_null_is_allowed`：兩筆 `book_key=None` 可並存；兩筆同一個非空 `book_key` 觸發 `IntegrityError`。
  - `test_deleting_a_recommendation_removes_its_course_rows`（模擬 validator / librarian 的直接 `DELETE recommendations`）。
  - `test_deleting_a_citation_removes_its_course_rows`（模擬 parser 先刪書目）。
  - `test_same_citation_cannot_be_linked_twice_to_one_recommendation`。
- [ ] **Step 2: 執行確認失敗**：預期 `AttributeError` / 欄位不存在。
- [ ] **Step 3: 實作** model 與 migration；`python -m alembic upgrade head` 套用，並驗證 `upgrade → downgrade -1 → upgrade` 往返。
- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_recommendation_schema.py -q`；`python -m pytest -q` 全部通過。
- [ ] **Step 5: Commit**：`git add src/library_agent/db/models.py alembic/versions/a7c4e1b9d3f2_add_book_key_and_recommendation_courses.py tests/test_recommendation_schema.py`，訊息 `feat: recommendations 加 book_key，新增 recommendation_courses`。

---

### Task 3: 合併計算與理由 prompt

**Files:** Modify `src/library_agent/agents/recommender.py`、`src/library_agent/prompts/recommender_prompts.py`；Test `tests/test_recommender_merge.py`

**Interfaces:**
- `@dataclass(frozen=True) class Member`：`holding_id: int, holdings_count: int, status: HoldingStatus, course_id: str, course_name: str, citation_id: int, is_required: bool, enrolled: int`。
- `@dataclass(frozen=True) class GroupDecision`：`representative: Member, enrolled_total: int, is_required: bool, status: HoldingStatus, current_copies: int, priority: PurchasePriority, copies: int, citation_ids: tuple[int, ...]`（排序後）。
- `merge_group(members: list[Member]) -> GroupDecision`：人數＝各不同 `course_id` 的 `enrolled` 加總（同課取一次）；`is_required = any`；`current_copies = max(holdings_count)`；狀態＝任一筆 `OWNED_EBOOK` 則 `OWNED_EBOOK`，否則代表者的狀態；代表者＝`holdings_count` 最大、同分取 `holding_id` 最小；`priority, copies = _decide(status, is_required, enrolled_total, current_copies)`。
- `_llm_rationale(title: str, course_lines: list[tuple[str, int, bool]], status: HoldingStatus, enrolled_total: int, current_copies: int, priority: PurchasePriority, copies: int) -> str`：`course_lines` 每項為 `(課名, 人數, 是否指定)`；prompt 逐行帶入「課名（人數、指定/參考）」並附合計；失敗時用模板。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_enrolment_is_summed_over_distinct_courses_once`：同一門課的兩個成員人數只算一次。
  - `test_holdings_are_deducted_once_not_per_member`：三門課各 50 人（各需 2 本）、現有 3 本 → 合併 `copies == 3`（逐列算會是 0）。
  - `test_any_required_makes_the_book_required`：一指定一參考 → 館藏不足時優先級為 MEDIUM（不是 LOW）。
  - `test_any_ebook_owned_skips_the_whole_book`
  - `test_representative_is_the_largest_holding_then_smallest_id`
  - `test_all_enrolments_unknown_defaults_to_one_copy`
  - `test_cap_applies_to_the_merged_total`（預設上限 5）。
  - `test_rationale_prompt_lists_each_course_with_its_own_category`：monkeypatch `R._client.chat.completions.create` 記錄 prompt，含兩門課各自的人數與「指定」「參考」字樣；**回應不得被硬編成『兩門都指定』**由 prompt 內容保證。
  - `test_rationale_falls_back_to_template_when_llm_fails`
- [ ] **Step 2: 執行確認失敗**：預期 `AttributeError`（`merge_group` 不存在）。
- [ ] **Step 3: 實作** `Member`、`GroupDecision`、`merge_group` 與新的 `_llm_rationale`、prompt 模板（課程改成逐行清單）。`recommender_node` 暫不動（Task 4）；為避免它呼叫舊簽章而崩，本任務先把節點內呼叫處暫時維持可運作或同一 commit 內一併調整成單一課程清單。
- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_recommender_merge.py tests/test_recommender.py -q`；`python -m pytest -q` 全部通過。
- [ ] **Step 5: Commit**：`feat: recommender 合併計算 merge_group 與逐課類別的理由 prompt`。

---

### Task 4: recommender 節點改寫

**Files:** Modify `src/library_agent/agents/recommender.py`（`recommender_node`）；Test `tests/test_recommender_node.py`

**Interfaces:**
- Consumes: `book_identity.assign_book_keys`、`book_identity.llm_judge`、`merge_group`、`_llm_rationale`、`RecommendationCourse`。
- `recommender._make_judge() -> Callable[[dict, dict], bool]`：建立 OpenAI client 並回傳 `llm_judge(...)`（測試以 monkeypatch 取代）。
- 行為：讀**全校**可採購的館藏（忽略 state 的 `course_ids`）；依 `book_key` 分組；每組 `merge_group`；與既有同 `book_key` 的列比對簽章 `(priority, copies, citation_ids)`：相同 → 不呼叫 `_llm_rationale`，只刷新代表欄位與關聯表；不同 → 重寫理由並**就地更新**（保留 id），新書則插入；關聯表每組整批重寫；結束時刪除 `book_key` 不在本次集合或為 NULL 的列；每 500 組 commit 一次；仍印出每筆摘要與優先級統計。

- [ ] **Step 1: 寫失敗的測試**（真實 DB 播種專用課程 `TESTRN*`，`_make_judge` 與 `_llm_rationale` 以 monkeypatch 取代並計次，`finally` 清理）：
  - `test_same_isbn_in_two_courses_becomes_one_recommendation`：一筆建議、兩筆關聯、冊數用合計人數、`book_key == "isbn:..."`。
  - `test_same_course_listing_a_book_twice_counts_its_enrolment_once`
  - `test_priority_follows_the_strictest_course`
  - `test_rerun_without_changes_calls_no_llm_and_keeps_the_id`
  - `test_changed_enrolment_updates_in_place_and_rewrites_the_rationale`
  - `test_stale_and_legacy_recommendations_are_removed`：不再可採購的書、以及 `book_key` 為 NULL 的舊格式列都被清掉。
  - `test_scoped_run_still_uses_whole_school_totals`：state 帶 `course_ids=[其中一門]`，合計仍含另一門課，且另一門課的建議沒被刪。
  - `test_books_with_the_same_invalid_isbn_but_different_authors_stay_separate`：`judge` 回 False → 兩筆建議。
  - `test_blocked_books_are_excluded_from_the_group`：`pairing_verdict='mismatch'` 且未放行者不納入。
- [ ] **Step 2: 執行確認失敗**：預期 FAIL（節點仍是一筆一列）。
- [ ] **Step 3: 實作** 節點。
- [ ] **Step 4: 執行確認通過**：`python -m pytest tests/test_recommender_node.py -q`；`python -m pytest -q` 全部通過。
- [ ] **Step 5: Commit**：`feat: recommender 依書合併，一本書一筆建議（全校計算、冪等、就地更新）`。

---

### Task 5: 讀取端（dashboard、報表、匯出）

**Files:** Modify `src/library_agent/dashboard.py`、`src/library_agent/main.py`；Modify `tests/test_dashboard_filters.py`（播種改成寫 `book_key` 與 `RecommendationCourse`）

**Interfaces:**
- `dashboard.RecRow`（`NamedTuple`）：`priority: str, courses: list[CourseRef], title: str, status: str, copies: int, rationale: str`；`CourseRef`（`NamedTuple`）：`name: str, key: str`（`required|optional|ai`）`, in_scope: bool`。
- `_load_data(departments)` 的 `recs` 改為 `list[RecRow]`（一本書一筆）；優先級圖、館藏狀態圖、`recs` 都從 `recommendations` 出發（代表館藏取狀態）；學系篩選改為 `EXISTS (關聯表中有課屬於所選學系)`；`in_scope` 在有篩選時標出屬於所選學系的課。移除 `_dedup_course_book`。
- `_rec_table(recs, pending)`：「課程」欄逐一列出 `名稱（指定|參考|AI推薦）`，`data-btypes` 為該書所有 `key` 的去重清單，類別欄標出各種類別。
- `main._fetch_report_rows()`：一本書一筆，`course_name` 為各課名稱以「、」串接，`is_required` 為任一課指定。
- `/export/recommendations`：欄位同前，「課程」「類別」改為多值串接。

- [ ] **Step 1: 寫失敗的測試**（先更新既有播種 fixture，使其寫 `book_key` 與關聯列）：
  - `test_recs_are_one_row_per_book_with_all_courses`：兩門課同一本書 → `recs` 一筆、`courses` 兩項。
  - `test_charts_and_table_share_the_same_base`：優先級圖加總＝`recs` 筆數＝狀態圖加總。
  - `test_department_filter_shows_a_shared_book_with_whole_school_copies`：書被 A、B 兩學系的課用到，選 A 時仍顯示，`copies` 為合計，只有 A 的課 `in_scope`。
  - `test_rec_table_marks_every_course_category_and_multiple_btypes`：一指定一參考 → 同一列 `data-btypes="required optional"`，且兩個類別篩選按鈕都能留住它（jsdom 驗證見 Step 5）。
  - `test_report_rows_join_course_names`（`main._fetch_report_rows`）。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作** 上述介面。
- [ ] **Step 4: 執行確認通過**：`python -m pytest -q` 全部通過。
- [ ] **Step 5: 瀏覽器行為驗證**：用 jsdom 載入真實頁面，重跑類別 × 優先級的組合驗證（`data-btypes` 多 token 時，選「指定」「參考」兩個類別都應顯示該列）。
- [ ] **Step 6: Commit**：`feat: dashboard、報表與匯出改讀「一本書一筆」的採購建議`。

---

### Task 6: 上線與核對（手動）

- [ ] **Step 1:** 備份：`docker exec library_agent-postgres-1 pg_dump -U postgres library_agent > <備份檔>`，確認檔案大小合理。
- [ ] **Step 2:** `python -m alembic upgrade head`（Task 2 已套用則確認 `alembic current`）。
- [ ] **Step 3:** 背景執行 `python src/library_agent/agents/recommender.py`，全程可由 dashboard 的執行紀錄或輸出檔觀察；預估約 40 分鐘（1544 筆理由，估計值）。
- [ ] **Step 4:** 核對：`recommendations` 筆數約 1544（±數筆）；`recommendation_courses` 約 1925；`book_key` 為 NULL 的列為 0；任選三本多課的書與手算對照；優先級圖加總＝館藏狀態圖加總＝明細筆數。
- [ ] **Step 5:** 重啟 dashboard，確認畫面；記錄冊數總和供上限決策（上限 5 時模擬為 2824）。
