# 人工審核區塊（第一步，唯讀）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 儀表板新增「人工審核」區塊，把待審書目分類、合併成「一組一本書」、依影響排序、可展開看證據；採購表標出「未驗證」。

**Architecture:** 新純函式模組 `review_groups`（分類與分組，不碰資料庫）；`_load_data` 多帶回分組結果與每筆建議的「未驗證」旗標；追溯模組的待審版本改成可吃多個書目；`dashboard` 渲染審核卡片並調整採購表；前端沿用既有的展開機制。

**Tech Stack:** Python、SQLAlchemy、FastAPI（單檔 dashboard，f-string 產生 HTML）、pytest（開發庫播種專用資料、`isolated_db`）、jsdom 驗證瀏覽器行為。

**Spec:** `docs/superpowers/specs/2026-10-31-review-panel-design.md`（第一步）

## Global Constraints

- 只讀：不寫資料庫、不加任何核准／退回／輸入動作。
- 第三方文字（書名、作者、理由、課名）進 HTML 一律 `html.escape`。
- 分組鍵的書名用 `match_auditor._norm` 正規化；**配錯的鍵必須含配到的書名**。
- 同一門課在一組內只算一次人數（以 `course_id` 去重）。
- 採購表任何新標記都不得含文字節點（匯出 CSV 讀 `textContent`）：用 CSS 畫。
- HTML 是 f-string：JS/CSS 大括號寫成雙括號；**含反斜線的程式碼一律用 Write/Edit 工具寫入**（Bash heredoc 會把雙反斜線收成單個）。
- 在 `isolated_db` 以外的測試只能讀開發庫、播種專用資料並清掉。
- `git add` 一律列明確路徑；`dashboard.py`、`trace_view.py` 可能有使用者未提交的區塊，用 `scratchpad/stage_mine.py` 或先 `git diff` 確認；不提交 `nla.py`、`runlog.py`。
- 現有 222 個測試維持通過；因移除待審列而必須改的測試要明確改並在 commit 說明。

## Review Focus

- 書名相同、配到不同本的兩筆配錯不得合併成一組。→ Task 1
- 同一門課列了同一本書兩次：人數只算一次。→ Task 1
- 某分頁、整個區塊沒有任何待審：不報錯、顯示說明。→ Task 3
- 學系篩選後某分頁為空。→ Task 2、3
- 追溯請求的 id 為空、非數字、過多，或其中部分已被處理：回 400／404／只顯示仍待審的，不得 500。→ Task 2
- 理由、書名含 `<script>` 或引號：不得變成可執行的 HTML。→ Task 3
- 「未驗證」標記不污染匯出的 CSV。→ Task 4

## File Structure

- Create `src/library_agent/review_groups.py`；Test `tests/test_review_groups.py`
- Modify `src/library_agent/trace_view.py`（`build_pending_trace` 吃多個 id）；Test `tests/test_trace_view.py`
- Modify `src/library_agent/dashboard.py`（`_load_data`、`RecRow.unverified`、審核卡片、採購表、端點、CSS、JS）；Test `tests/test_dashboard_review.py`（新）、`tests/test_dashboard_dedup.py`、`tests/test_dashboard_filters.py`

---

### Task 1: 分類與分組（`review_groups`，純函式）

**Files:** Create `src/library_agent/review_groups.py`；Test `tests/test_review_groups.py`（不需資料庫）

**Interfaces:**（全部 `@dataclass(frozen=True)`）
- `PendingItem(vb_id, citation_id, course_id, course_name, enrolled, title, authors: list[str], matched_title, matched_authors: list[str], source, verified: bool, verdict, judge, reason, confidence: float)`
- `ReviewGroup(kind, title, authors, matched_title, matched_authors, source, judge, reason, low_confidence: bool, courses: list[tuple[str, int]], items: int, enrolled_total: int, vb_ids: tuple[int, ...], priority: str | None, copies: int | None)`
- `KINDS = ("mismatch", "notfound", "other")`
- `classify(item: PendingItem, threshold: float) -> str`：`verdict == "mismatch"` → `mismatch`；否則 `not verified` → `notfound`；否則 `other`。
- `group_pending(items: list[PendingItem], rec_of: dict[int, tuple[str, int]], threshold: float) -> dict[str, list[ReviewGroup]]`：`rec_of` 是 `citation_id → (優先級, 冊數)`；回傳每個 `kind` 一個已排序的清單（三個鍵都在，可為空）。分組鍵：`mismatch` ＝（`_norm(title)`, `_norm(matched_title)`）；`notfound` ＝ `_norm(title)`；`other` ＝ `vb_id`。`courses` 以 `course_id` 去重並依課名排序；`low_confidence` ＝ 組內任一筆信心低於門檻；`priority`／`copies` 取組內成員在 `rec_of` 中最嚴重者（優先級 high < medium < low < skip），都不在則為 `None`。排序：`notfound` 依（優先級順序，不在清單排最後；人數合計大到小；書名），其餘依（人數合計大到小；書名）。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_mismatch_with_same_title_but_different_matched_books_stay_separate`：兩筆原書名相同（*Macroeconomics*）、配到不同書 → 兩組。
  - `test_mismatch_with_same_original_and_matched_merge_into_one_group`：兩門課、同原書同配到的書 → 一組，`items == 2`、`vb_ids` 兩個。
  - `test_notfound_groups_by_original_title_only`、`test_other_items_never_merge`。
  - `test_enrolment_counts_each_course_once`：同一門課兩個書目 → `courses` 一項、`enrolled_total` 不翻倍。
  - `test_classify_mismatch_wins_over_notfound`。
  - `test_notfound_group_carries_the_most_severe_priority_of_its_members`、`test_group_not_in_any_recommendation_has_no_priority`。
  - `test_notfound_sorted_by_priority_then_enrolment`、`test_mismatch_sorted_by_enrolment_desc`。
  - `test_low_confidence_flag_and_empty_input`：空輸入回三個空清單。
  - `test_title_normalization_merges_case_and_width_variants`。
- [ ] **Step 2: 執行確認失敗**：`python -m pytest tests/test_review_groups.py -q`，預期 ImportError。
- [ ] **Step 3: 實作** `review_groups.py`（正規化直接 `from library_agent.agents.match_auditor import _norm`）。
- [ ] **Step 4: 執行確認通過**。
- [ ] **Step 5: Commit**：`git add src/library_agent/review_groups.py tests/test_review_groups.py`，訊息 `feat: review_groups 把待審書目分類並合併成一組一本書`。

---

### Task 2: 資料層與多書目追溯

**Files:** Modify `src/library_agent/dashboard.py`、`src/library_agent/trace_view.py`；Test `tests/test_dashboard_review.py`（新，開發庫播種專用資料，仿 `tests/test_dashboard_dedup.py`）、`tests/test_trace_view.py`

**Interfaces:**
- `dashboard._load_data` 回傳新增鍵 `"review": dict[str, list[ReviewGroup]]`（`group_pending` 的結果；套學系篩選；`rec_of` 只含可採購的建議）；既有的 `"pending"` 不動（`/export/pending` 與 KPI 仍用）。
- `RecRow` 新增 `unverified: bool = False`（放最後）：這本書的任一書目 `verified=False`。
- `trace_view.build_pending_trace(session, vb_ids: list[int]) -> Trace | None`：依序蒐集仍為 `pending` 的書目；課綱依課去重、來源每書目一列、配對依 `_pairing_key` 合併；館藏取冊數最大者；`pending_reason` 為各書目原因去重後以「；」連接；全部不在待審則回 `None`；標題為第一個書目的原書名。
- `GET /trace/pending?ids=1&ids=2`：取代 `GET /trace/pending/{vb_id}`；沒有 id 或超過 50 個回 400；`build_pending_trace` 回 `None` 回 404。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_load_data_exposes_review_groups_by_kind`（播種一筆配錯、一筆查無此書）、`test_review_groups_respect_the_department_filter`、`test_notfound_group_shows_its_recommendation_priority`、`test_recrow_marks_unverified_books`。
  - `test_pending_trace_merges_several_items_of_one_group`（兩個書目、兩門課 → 課綱 2 份、來源 2 列、相同配對 1 項）、`test_pending_trace_skips_items_no_longer_pending`、`test_pending_trace_all_resolved_returns_none`。
  - 端點：`test_pending_trace_endpoint_accepts_several_ids`、`test_pending_trace_endpoint_400_without_ids_or_too_many`、`test_pending_trace_endpoint_404_when_nothing_pending`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**：待審查詢多選配到的書名／作者、來源、`verified`、判定欄、課程 id 與人數（以 `Course.enrolled_count`）；另查 `recommendation_courses` 取每個書目所在建議的優先級與冊數（只含 `purchasable()` 的建議）；用 `ReviewGroup` 與 `RecRow.unverified` 帶出。把 `build_pending_trace` 改成多 id、刪掉單一 id 的路徑端點。信心門檻取 `get_settings().human_review_confidence_threshold`。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過（`tests/test_trace_view.py` 與 `tests/test_dashboard_dedup.py` 中舊的單一 id 待審追溯測試要明確改成新介面）。
- [ ] **Step 5: Commit**（`dashboard.py` 用 `stage_mine.py` 排除使用者的區塊）：`feat: 待審資料分組、建議的未驗證旗標與多書目追溯`。

---

### Task 3: 渲染審核卡片與調整採購表

**Files:** Modify `src/library_agent/dashboard.py`；Test `tests/test_dashboard_review.py`、`tests/test_dashboard_dedup.py`、`tests/test_dashboard_filters.py`

**Interfaces:**
- `_review_panel(review: dict[str, list[ReviewGroup]]) -> str`：`<section class="card" id="review">`；三個分頁按鈕 `<button class="rtab" data-k="mismatch|notfound|other">…</button>` 與三個面板 `<div class="rpanel" data-k="…">`；每組一列 `<tr data-ids="1,2" …>`，含展開鈕（`class="tbtn"`、不含文字）。分頁標示「N 組（M 筆書目）」。完全沒有待審時顯示「目前沒有待審書目」；某分頁沒有組時顯示一句說明。標頭有指向 `/export/pending` 的連結。
- `_rec_table(recs: list) -> str`：移除 `pending` 參數與待審列。
- `_rec_row(..., unverified: bool = False)`：`True` 時 `<tr>` 加 `data-unverified="1"`，書名格後加 `<span class="unv" title="驗證階段查無此書，館藏是用書名直接查詢的結果"></span>`（標記文字由 CSS `::after` 畫）。
- 優先級篩選的「待審核」按鈕改成 `<a href="#review">`；匯出面板移除「待審核」勾選項；KPI「待人工審核」卡片加 `href="#review"` 連結。

- [ ] **Step 1: 寫失敗的測試**：
  - `test_review_panel_has_three_tabs_with_group_and_item_counts`、`test_review_panel_lists_mismatch_with_both_books_and_reason`、`test_review_panel_notfound_shows_no_record_and_priority`、`test_review_panel_empty_states`（整個為空、單一分頁為空）、`test_review_panel_escapes_untrusted_text`（`<script>`、引號）。
  - `test_rec_table_no_longer_contains_pending_rows`、`test_unverified_recommendation_carries_a_text_free_marker`（`data-unverified`、`class="unv"` 且標籤之間沒有文字）。
  - `test_priority_filter_pending_button_links_to_the_review_panel`、`test_export_panel_has_no_pending_checkbox`。
- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作**（CSS 沿用 `.tag`／`.pill`／`--border`；`.unv::after` 用 `content:"未驗證"`；分頁樣式沿用 `.fbtn`）。更新受影響的既有測試（`test_dashboard_filters.py` 的 `_rec_table(recs, pending)`、`test_dashboard_dedup.py` 的待審列測試）並在 commit 說明原因。
- [ ] **Step 4: 執行確認通過**；`python -m pytest -q` 全過。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: dashboard 新增人工審核區塊，採購表標出未驗證`。

---

### Task 4: 前端互動、驗證與文件

**Files:** Modify `src/library_agent/dashboard.py`（JS）、`CLAUDE.md`

**Interfaces:**
- 分頁切換：點 `.rtab` → 只顯示對應的 `.rpanel`，更新 `active`；預設顯示筆數最多的分頁（都為空則第一個）。
- 展開：審核表的 ▸ 與採購表共用同一套委派；有 `data-ids` 的列向 `/trace/pending?ids=…`（逗號分隔轉成重複的 `ids` 參數）取，其餘行為不變（只取一次、可重試）。
- 篩選／搜尋／全選／匯出對採購表的行為完全不變（審核表不在 `#rectable` 內）。

- [ ] **Step 1: 寫 jsdom 驗證腳本（暫存區，不進版本庫）並看它失敗**：驗證分頁切換與預設分頁；審核列展開呼叫 `/trace/pending?ids=…` 且只一次、取回失敗可重試；採購表的篩選、搜尋、全選、匯出 CSV 照舊；**匯出的 CSV 不含「未驗證」標記文字**；「待審核」按鈕是連結而不是篩選。
- [ ] **Step 2: 實作** JS（注意雙括號）。
- [ ] **Step 3: 執行確認通過**：jsdom 腳本全過；`python -m pytest -q` 全過。
- [ ] **Step 4: 真實資料核對**：對開發庫產生審核區塊，確認三個分頁的組數與筆數為 537 組／746 筆、426 組／532 筆、其他 2 筆（以當下實際資料為準，與 SQL 直接統計對照）；讀一組配錯與一組查無此書的展開內容；確認採購表的「未驗證」數量為 426 本。
- [ ] **Step 5: Commit**（`stage_mine.py`）：`feat: 審核區塊的分頁切換與展開`；並在 `CLAUDE.md` 簡述審核區塊、`/trace/pending?ids=`、「未驗證」標記，以及「第二步（動作）尚未做」。
