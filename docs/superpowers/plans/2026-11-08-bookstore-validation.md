# 購物車決定搬移與書商後備驗證 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 書的識別鍵改變時購物車決定跟著搬（搬不了的看得到），並讓 validator 在原有來源查不到時用 TAAZE、誠品補驗證。

**Architecture:** 第一部分在 `purchase_decisions` 記下書名與課綱書目 id，`recommender.recommend()` 結束時對帳搬移；`/cart` 多回「找不到的決定」。第二部分新增 `integrations/taaze.py`（httpx＋BeautifulSoup）、`integrations/eslite.py`（Playwright 驅動 Edge），以 match_auditor 抽出的 `pick_same_book` 判斷同一本，接在 validator 回傳 not_found 之前。

**Tech Stack:** Python 3.11+、SQLAlchemy 2、Alembic、PostgreSQL、FastAPI、httpx、BeautifulSoup、Playwright（`channel="msedge"`）、OpenAI 相容 API、pytest。

**Spec:** `docs/superpowers/specs/2026-11-08-bookstore-validation-design.md`

## Global Constraints

- 實作順序：Task 1–3（第一部分）全部完成後才做 Task 4–8（第二部分）。
- 寫入資料庫的測試一律用 `isolated_db`（`<開發庫>_test`），**不要**對開發庫做真實決定或跑 recommender。
- TLS 憑證檢查保持開啟，**不可** `verify=False`；TAAZE 只是在 certifi 之外額外載入 Sectigo 中繼憑證。
- 每家書商同一時間只有一個請求，兩次請求間隔 1 秒 × 隨機 0.7–1.3。
- 判斷同一本：`RULE_THRESHOLD = 0.6`；每次查詢最多讓 LLM 判斷前 3 名；LLM 失敗＝不同本（`fail_as_same=False`）；模型 `LLM_MODEL_MINI`、temperature 0。
- 每家最多兩次查詢：完整書名，必要時主書名；判斷一律用課綱的完整書名與作者。
- 設定 `BOOKSTORE_SOURCES`，預設 `"taaze,eslite"`，空字串＝關閉。
- 不使用 API Anything；不送任何資料到外部目錄。
- 畫面用詞只用「採購建議」「購物車」（見 CLAUDE.md「用詞」）。
- commit 前看 pytest **自己的**結束碼（不要用 `| tail` 吞掉）；`dashboard.py` 有使用者未提交的 `_collectChecked` 行尾空白，commit 時用暫存區的 `stage_mine.py` 排除那個區塊。
- commit 訊息結尾：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Review Focus

1. 同一次對帳裡，兩筆舊決定的書目都指向同一本新書 → 先處理的（`decided_at` 較新者）搬過去，另一筆成為「找不到的決定」，不覆蓋、不撞 `book_key` 唯一鍵（Task 2）。
2. 課綱沒有作者、書商有兩筆以上正規化後同名的書（通用教科書名如「經濟學」）→ 不判驗證成功，維持 not_found（Task 7）。
3. 誠品瀏覽器卡住 → 單次查詢 30 秒逾時後回空結果並停用誠品，validator 不會卡住（Task 6）。
4. validator 的兩個執行緒同時查同一家 → 兩次請求的間隔仍 ≥ 0.7 秒（Task 5）。
5. 書名含 `&`、`#`、`%`、`+`、引號 → 查詢參數正確編碼，不截斷、不變成另一個參數（Task 5、Task 6）。

---

## 檔案結構

| 檔案 | 責任 |
|---|---|
| `alembic/versions/f5b7c9d2e4a8_add_decision_snapshot.py`（新） | `purchase_decisions` 加 `title`、`citation_ids`，並用目前採購建議補上現有決定 |
| `src/library_agent/purchase_reconcile.py`（新） | 決定的快照（書名、書目 id）、對帳搬移、找出「找不到的決定」 |
| `src/library_agent/db/models.py` | `PurchaseDecision` 加兩欄 |
| `src/library_agent/dashboard.py` | 兩個決定端點寫快照；`/cart` 回 `orphans`；抽屜顯示「找不到對應的書」 |
| `src/library_agent/agents/recommender.py` | `recommend()` 最後呼叫對帳 |
| `src/library_agent/agents/match_auditor.py` | 抽出 `rule_verdict`、`pick_same_book`、`bookstore_llm` |
| `src/library_agent/integrations/bookstore.py`（新） | `StoreRecord`、`Pacer`、`main_title`、作者切分 |
| `src/library_agent/integrations/taaze.py`（新） | TAAZE 搜尋與解析 |
| `src/library_agent/integrations/eslite.py`（新） | 誠品搜尋（專用執行緒＋Playwright）與解析 |
| `src/library_agent/integrations/certs/sectigo_public_server_auth_ca_ev_r36.pem`（新） | TAAZE 缺的中繼憑證 |
| `src/library_agent/agents/validator.py`、`state.py`、`config.py` | 書商後備、稽核三欄、設定 |
| `src/library_agent/trace_view.py`、`dashboard.py` | 來源標籤 |
| `experiments/bookstore_dryrun.py`（新） | 只讀試跑：對現有查無此書跑書商後備、印命中數 |
| `tests/fixtures/`（新） | TAAZE 搜尋頁（有結果、查無結果）、誠品 JSON |

---

## 第一部分：購物車決定搬移

### Task 1: 決定的快照（書名、課綱書目 id）

**Files:**
- Create: `alembic/versions/f5b7c9d2e4a8_add_decision_snapshot.py`（`down_revision = 'c4a9e7d1f2b6'`）
- Create: `src/library_agent/purchase_reconcile.py`
- Modify: `src/library_agent/db/models.py`（`PurchaseDecision`）、`src/library_agent/dashboard.py`（`purchase_decide`、`purchase_decide_many`）
- Test: `tests/test_purchase_snapshot.py`

**Interfaces:**
- Produces: `PurchaseDecision.title: str | None`（Text）、`PurchaseDecision.citation_ids: str | None`（Text，JSON 整數陣列，遞增排序）
- Produces: `purchase_reconcile.snapshot(session, book_key: str) -> tuple[str | None, list[int]]`：該鍵目前的採購建議的書名（代表書目的 `buy_title`）與 `recommendation_courses.citation_id`（排序、去重）；沒有建議回 `(None, [])`
- Produces: migration 模組內的 `BACKFILL_SQL: str`（Postgres；`title` 取代表書目的 `coalesce(purchase_title, canonical_title)`，`citation_ids` 用 `json_agg(... ORDER BY ...)::text`），`upgrade()` 加欄位後執行它

- [ ] **Step 1: 寫失敗的測試**（沿用 `tests/test_purchase_endpoint.py` 的 `add_rec`、`client` 寫法；`add_rec` 建的建議沒有 `recommendation_courses`，測試要自己補一列）

```python
def test_decide_stores_the_title_and_the_citation_ids(client, isolated_db): ...
    # 建一本書（2 筆課綱書目都連到同一筆建議）→ POST /purchase/decide copies=3
    # assert row.title == "Intro" and json.loads(row.citation_ids) == sorted([cit1, cit2])
def test_decide_many_stores_snapshots_for_each_book(client, isolated_db): ...
def test_redeciding_refreshes_the_snapshot(client, isolated_db): ...      # 之後多一門課 → 再決定一次，citation_ids 多一個
def test_clearing_deletes_the_row_and_needs_no_snapshot(client, isolated_db): ...
def test_backfill_sql_fills_existing_decisions_and_leaves_unmatched_null(isolated_db): ...
    # 用 importlib 依路徑載入 migration 檔，對 isolated_db 執行 BACKFILL_SQL
    # 對得上建議的決定補上 title/citation_ids；book_key 對不上任何建議的維持 NULL
```

- [ ] **Step 2: 跑測試確認失敗** — `python -m pytest tests/test_purchase_snapshot.py -q -p no:cacheprovider` → FAIL（欄位不存在）

- [ ] **Step 3: 實作** model 兩欄、migration（含 `downgrade()` 刪兩欄）、`snapshot()`、兩個端點在 upsert 時一併寫 `title`／`citation_ids`（`on_conflict_do_update` 也更新這兩欄）

- [ ] **Step 4: 跑測試確認通過**，再跑 `tests/test_purchase_endpoint.py tests/test_purchase_batch.py tests/test_purchase_schema.py` 確認既有行為不變 → PASS

- [ ] **Step 5: 對開發庫升級（先備份）**

```bash
docker exec library_agent-postgres-1 pg_dump -U postgres library_agent > ~/backups/library_agent_before_decision_snapshot.sql
alembic upgrade head
```
Expected：備份檔大於 0；升級無錯；查開發庫，現有 3 筆決定中對得上建議的都有 `title` 與 `citation_ids`（唯讀查詢印出來）。

- [ ] **Step 6: Commit**（`feat: 購物車決定記下書名與課綱書目 id，供識別鍵改變時搬移`）

### Task 2: 對帳搬移

**Files:**
- Modify: `src/library_agent/purchase_reconcile.py`、`src/library_agent/agents/recommender.py`（`recommend()` 最後的 `session.commit()` 之後）
- Test: `tests/test_purchase_reconcile.py`

**Interfaces:**
- Consumes: Task 1 的兩欄與 `snapshot()`
- Produces: `purchase_reconcile.reconcile(session) -> ReconcileResult`；`ReconcileResult` 是 NamedTuple：`refreshed: int, moved: int, orphaned: int`。由呼叫端 commit。
- 規則（依序處理決定，`decided_at` 新的先）：
  - 鍵還在 → 用 `snapshot()` 更新 `title`、`citation_ids`（`refreshed`）。
  - 鍵不在 → 取 `citation_ids` 中**仍屬於某筆建議**的書目，它們所屬的鍵：恰好 1 個、且該鍵沒有決定 → 改 `book_key`，`copies`、`suggested_at_decision`、`decided_at` 不動，`title`／`citation_ids` 換成新書的（`moved`）；其餘情況（0 個、≥2 個、新鍵已有決定、`citation_ids` 為 NULL）不動（`orphaned`）。
- `recommend()` 印一行：`購物車對帳：搬移 {moved} 筆、更新 {refreshed} 筆、找不到 {orphaned} 筆`。

- [ ] **Step 1: 寫失敗的測試**（用 `tests/test_recommender_node.py` 的 `node` fixture、`seed`、`book`；決定直接插 `PurchaseDecision`）

```python
def test_unchanged_key_only_refreshes_the_snapshot(node): ...
def test_changed_key_moves_the_decision_and_keeps_copies_and_the_suggestion_then(node): ...
    # seed 一本無 ISBN 的書 → recommender_node → 決定 copies=3、suggested_at_decision=原建議、快照
    # 把 verified_books.isbn_13 改成有效 ISBN（模擬書商補上）→ recommender_node
    # assert 唯一的決定 book_key == "isbn:<13碼>" and copies == 3 and suggested_at_decision 不變
def test_target_that_already_has_a_decision_is_not_overwritten(node): ...
def test_two_old_decisions_merging_into_one_book_move_only_the_newer_one(node): ...   # Review Focus 1
def test_book_split_into_two_is_left_as_an_orphan(node): ...
def test_book_that_left_the_list_is_left_as_an_orphan(node): ...       # 例如 review_status 改成 rejected
def test_decision_without_citation_ids_is_left_alone(node): ...
def test_scoped_recommend_also_reconciles(node): ...                   # R.recommend(only_citation_ids={...})
```

- [ ] **Step 2: 跑測試確認失敗** — `python -m pytest tests/test_purchase_reconcile.py -q -p no:cacheprovider` → FAIL（`reconcile` 不存在）

- [ ] **Step 3: 實作 `reconcile()` 並在 `recommend()` 呼叫**：一次查出目前所有建議的 `book_key` 與 `citation_id → book_key` 對照，記憶體內決定、一次寫回；搬移時同一輪內「已被佔用的新鍵」要算進去（Review Focus 1）。

- [ ] **Step 4: 跑測試確認通過**，再跑 `tests/test_recommender_node.py tests/test_recommender_scope.py tests/test_dashboard_decisions.py` → PASS

- [ ] **Step 5: Commit**（`feat: 重算採購建議後對帳，識別鍵改變時購物車的決定跟著搬`）

### Task 3: 「找不到的決定」看得到

**Files:**
- Modify: `src/library_agent/purchase_reconcile.py`、`src/library_agent/dashboard.py`（`cart()`、抽屜標記、`_renderCart`、抽屜事件）、`CLAUDE.md`（「購物車與冊數決定」的限制段落）
- Test: `tests/test_cart_endpoint.py`、`tests/test_dashboard_tabs.py`

**Interfaces:**
- Produces: `purchase_reconcile.orphans(session) -> list[dict]`：`copies ≥ 1`、`book_key` 不在任何 `purchasable()` 的建議裡的決定，`{"book_key", "title", "copies"}`，依 `decided_at` 新到舊
- Produces: `GET /cart` 回應多 `"orphans": [...]`；`books`／`copies` 合計**不含**它們
- Produces: 抽屜標記 `<div id="cartorphans" hidden>`（在 `#cartlist` 之後）；每筆有書名、冊數與 `<button data-orphan-remove>移除</button>`（呼叫 `/purchase/decide-many` mode `clear`）；有孤兒時標題「找不到對應的書（N）」與一句說明「這些決定的書已不在採購建議裡，可能是書目更新後對不上。」

- [ ] **Step 1: 寫失敗的測試**

```python
def test_cart_lists_orphans_with_title_and_copies_but_does_not_count_them(client, isolated_db): ...
    # 一筆正常決定 copies=2、一筆 book_key 對不上的決定 copies=5（title="舊書名"）、一筆對不上的 copies=0
    # assert body["books"] == 1 and body["copies"] == 2
    # assert body["orphans"] == [{"book_key": ..., "title": "舊書名", "copies": 5}]
def test_cart_has_no_orphans_key_entries_when_everything_matches(client, isolated_db): ...   # orphans == []
def test_the_drawer_has_a_hidden_orphan_section():                       # tests/test_dashboard_tabs.py
    # markup 有 id="cartorphans" 且帶 hidden，位置在 id="cartlist" 之後
```

- [ ] **Step 2: 跑測試確認失敗** → FAIL

- [ ] **Step 3: 實作** `orphans()`、`/cart` 加欄位、抽屜標記與 JS（書名用 `textContent`；移除後重取 `/cart`）、CLAUDE.md 改寫限制段落為搬移規則

- [ ] **Step 4: 跑測試確認通過**；用暫存區的 jsdom 腳本（仿 `cartdrawercheck.cjs`，假 `/cart` 回一筆孤兒）確認：區塊看得見、標題含「（1）」、按「移除」送出 clear 並在重取後消失；整個 `pytest` → PASS

- [ ] **Step 5: Commit**（`feat: 購物車抽屜顯示「找不到對應的書」的決定`）

---

## 第二部分：書商後備驗證

### Task 4: 共用的「是不是同一本」判斷

**Files:**
- Modify: `src/library_agent/agents/match_auditor.py`
- Test: `tests/test_same_book_pick.py`

**Interfaces:**
- Produces: `rule_verdict(orig_title: str, cand_title: str) -> str | None`：`rule_score < RULE_THRESHOLD` 且非 `_cross_script` → `"mismatch"`；否則 `None`（要問 LLM）。`match_auditor_node` 改用它（行為不變）。
- Produces: `pick_same_book(orig_title: str, orig_authors: list[str], candidates: list[tuple[str, list[str]]], llm: Callable[[str, list[str], str, list[str]], tuple[bool, str]], max_llm: int = 3) -> tuple[int | None, str]`：回傳採用的候選索引（或 None）與理由。排序：規則通過者依 `rule_score` 高到低，跨文字者排在其後；`rule_verdict` 為 `"mismatch"` 的不問。
- Produces: `bookstore_llm() -> Callable[[str, list[str], str, list[str]], tuple[bool, str]]`：包 `_llm_same_book(client, settings.llm_model_mini, ..., fail_as_same=False)`，client 在第一次呼叫時建立並重用。

- [ ] **Step 1: 寫失敗的測試**（`llm` 用假函式記錄呼叫）

```python
def test_rule_mismatch_candidates_are_never_sent_to_the_llm(): ...
def test_candidates_are_asked_in_rule_score_order_and_the_first_same_wins(): ...
def test_at_most_three_candidates_are_asked(): ...                      # 5 個都通過規則、LLM 都說不同 → 只呼叫 3 次、回 None
def test_cross_script_candidates_go_to_the_llm_after_rule_passing_ones(): ...
def test_llm_failure_counts_as_different(monkeypatch): ...              # bookstore_llm 的 client 拋例外 → (False, ...)
def test_no_candidates_returns_none(): ...
```

- [ ] **Step 2: 跑測試確認失敗** → FAIL；**Step 3: 實作**；**Step 4:** 本檔與 `tests/test_match_auditor*.py`（既有）、`tests/test_book_identity*.py` 全過

- [ ] **Step 5: Commit**（`refactor: 抽出 match_auditor 的同一本判斷，供書商後備共用`）

### Task 5: TAAZE

**Files:**
- Create: `src/library_agent/integrations/bookstore.py`、`src/library_agent/integrations/taaze.py`、`src/library_agent/integrations/certs/sectigo_public_server_auth_ca_ev_r36.pem`
- Create: `tests/fixtures/taaze_search_hit.html`、`tests/fixtures/taaze_search_empty.html`
- Test: `tests/test_bookstore_common.py`、`tests/test_taaze.py`

**Interfaces:**
- Produces（`bookstore.py`）：
  - `@dataclass(frozen=True) StoreRecord: title: str, authors: list[str], isbn_13: str | None, publisher: str, published: str, source: str`
  - `class Pacer(min_interval: float = 1.0)`，方法 `wait() -> None`：執行緒安全，兩次 `wait()` 返回的間隔 ≥ `min_interval × 隨機 0.7–1.3`
  - `main_title(title: str) -> str | None`：第一個分隔符號（`——`、`──`、`—`、`–`、`:`、`：`、` - `、`/`、`／`）前的部分，去頭尾空白；與原書名相同或少於 2 個字回 None
  - `split_authors(raw: str) -> list[str]`：去掉「作者：」前綴，以 `、`、`,`、`，`、`/`、`／`、`;` 切開並去空白
- Produces（`taaze.py`）：`SEARCH_URL = "https://www.taaze.tw/rwd_searchResult.html"`；`parse_search_html(html: str) -> list[StoreRecord]`（`div.media`；書名去掉結尾括號標記如「(二手書)」；`source="taaze"`；`isbn_13=None`）；`search(query: str) -> list[StoreRecord]`（params `{"keyType[]": "0", "keyword[]": query}`、模組層 `Pacer`、SSL context＝certifi＋憑證檔、timeout 20 秒、任何例外回 `[]`）
- 憑證檔：`curl http://crt.sectigo.com/SectigoPublicServerAuthenticationCAEVR36.crt` 後 `openssl x509 -inform DER` 轉 PEM；`openssl x509 -noout -subject` 應為 `CN=Sectigo Public Server Authentication CA EV R36`
- Fixture：各用一次 `curl` 取真實頁面（有結果：「被討厭的勇氣」；查無結果：一個不存在的書名），**裁到只剩 2–3 個 `div.media`**

- [ ] **Step 1: 寫失敗的測試**

```python
def test_main_title_cuts_at_the_first_separator(): ...      # "語言、社會與族群意識—台灣語言社會學研究" → "語言、社會與族群意識"；"基礎經濟學 / Essential…" → "基礎經濟學"
def test_main_title_is_none_when_nothing_to_cut_or_too_short(): ...
def test_split_authors_handles_prefix_and_separators(): ...  # "作者：岸見一郎、古賀史健" → ["岸見一郎", "古賀史健"]
def test_pacer_spaces_calls_from_two_threads(): ...           # Review Focus 4：min_interval=0.2，兩條執行緒各 wait 兩次 → 相鄰間隔 ≥ 0.14
def test_parse_hit_page(): ...                                # 書名不含「(二手書)」、作者、出版社、出版日期、source == "taaze"
def test_parse_empty_page_returns_empty_list(): ...
def test_search_encodes_special_characters(monkeypatch): ... # Review Focus 5：攔截 httpx 請求，"C++ & Java #1 \"引號\" 100%" 原樣出現在 keyword[] 參數值
def test_search_returns_empty_list_on_network_error(monkeypatch): ...
def test_ssl_context_loads_the_extra_intermediate(): ...      # context 的 get_ca_certs() 含該 CN
```

- [ ] **Step 2: 跑測試確認失敗** → FAIL；**Step 3: 實作**；**Step 4: 跑測試確認通過**（不連網）；再手動一次 `python -c "from library_agent.integrations import taaze; print(taaze.search('語言．社會與族群意識')[:1])"` 應印出黃宣範那本

- [ ] **Step 5: Commit**（`feat: TAAZE 搜尋（純 HTTP，補上 Sectigo 中繼憑證）`）

### Task 6: 誠品

**Files:**
- Create: `src/library_agent/integrations/eslite.py`、`tests/fixtures/eslite_search.json`
- Modify: `pyproject.toml`（dependencies 加 `"playwright>=1.45"`）
- Test: `tests/test_eslite.py`

**Interfaces:**
- Consumes: `StoreRecord`、`Pacer`、`split_authors`（Task 5）、`book_identity.normalize_isbn`
- Produces: `API_URL = "https://holmes.eslite.com/v1/search"`；`parse_search_json(data: dict) -> list[StoreRecord]`（`results` 中 `is_book == "yes"`；`authors` 每個字串再用 `split_authors`；`isbn10` → `normalize_isbn`；`publisher` 取 `manufacturers[0]`；`published` 取 `publish_time` 前 10 字；`source="eslite"`）
- Produces: `search(query: str, timeout: float = 30.0) -> list[StoreRecord]`：交給模組內 `max_workers=1` 的 executor；瀏覽器在該執行緒延遲啟動（`chromium.launch(channel="msedge", headless=True)`），先 `goto("https://www.eslite.com/")` 一次，之後 `page.evaluate` 以 `fetch()` 呼叫 `API_URL`（參數 `q`、`page_size=20`、`page_no=1`，用 `URLSearchParams` 組，不手拼字串；先不帶 `visitor_id`，手動驗證時若接口不回結果，再加一個每次啟動產生一次的隨機 32 字 hex）；逾時或任何例外 → 記一行 log、設 `_disabled = True`、回 `[]`；`_disabled` 時直接回 `[]`
- Produces: `close() -> None`（關閉瀏覽器與 executor，可重複呼叫）；`_launch_browser()` 是可被測試替換的啟動點
- Fixture：從暫存區試驗結果（`learn_eslite.json` 的 `data`）取 3 筆組成 `{"results": [...]}`，含一筆 `is_book: "no"`（手改）

- [ ] **Step 1: 寫失敗的測試**

```python
def test_parse_keeps_books_only_and_converts_isbn10(): ...    # 9861371958 → 9789861371950；is_book "no" 的被排除
def test_parse_splits_slash_joined_authors(): ...             # ["岸見一郎/ 古賀史健"] → ["岸見一郎", "古賀史健"]
def test_search_returns_empty_and_disables_when_launch_fails(monkeypatch): ...
def test_search_times_out_without_hanging(monkeypatch): ...   # Review Focus 3：_launch_browser 睡 5 秒、timeout=0.5 → 1 秒內回 []，之後 _disabled
def test_search_passes_the_query_as_a_parameter(monkeypatch): ...   # Review Focus 5：假 page.evaluate 收到的參數物件裡 q 等於原字串
def test_close_is_idempotent(): ...
```

- [ ] **Step 2: 跑測試確認失敗** → FAIL；**Step 3: 實作**；**Step 4: 跑測試確認通過**；再手動 `python -c "from library_agent.integrations import eslite; print(eslite.search('台灣電影饗宴')[:1]); eslite.close()"` 應印出含 ISBN 的一筆

- [ ] **Step 5: Commit**（`feat: 誠品搜尋（Playwright 驅動 Edge，在頁面裡呼叫 JSON 接口）`）

### Task 7: 接進 validator

**Files:**
- Modify: `src/library_agent/agents/validator.py`、`src/library_agent/state.py`（`VerifiedBook`）、`src/library_agent/config.py`
- Test: `tests/test_validator_bookstore.py`

**Interfaces:**
- Consumes: `pick_same_book`、`bookstore_llm`（Task 4）；`taaze.search`、`eslite.search`、`eslite.close`、`main_title`（Task 5–6）
- Produces: `config.Settings.bookstore_sources: str = "taaze,eslite"`
- Produces: `state.VerifiedBook` 加 `pairing_verdict: str | None = None`、`pairing_judge: str | None = None`、`pairing_reason: str | None = None`；`_stage_save` 寫進資料庫
- Produces: `validator._bookstore_fallback(citation: BookCitation, low_confidence: bool) -> VerifiedBook | None`：依 `bookstore_sources` 順序；每家先查完整書名，沒採用再查 `main_title`；採用時回 `VerifiedBook(canonical_title=r.title, canonical_authors=r.authors or citation.authors, isbn_13=citation.isbn or r.isbn_13, source=r.source, verified=True, requires_human_review=low_confidence, pairing_verdict="same", pairing_judge="llm", pairing_reason=理由)`
- 課綱沒有作者、候選中正規化書名（`match_auditor._norm`）等於該候選者有 ≥ 2 筆 → 這次查詢不採用（Review Focus 2）
- `_validate_chinese`／`_validate_english` 在回傳 not_found／ncl_not_found 之前呼叫它；`QuotaExceededError` 那條路不呼叫
- `validator_node` 結束時（`finally`）呼叫 `eslite.close()`

- [ ] **Step 1: 寫失敗的測試**（monkeypatch `nla_lookup`、`loc_lookup`、`google_lookup` 回 None；`taaze.search`／`eslite.search` 回假的 `StoreRecord`；`bookstore_llm` 回假函式）

```python
def test_taaze_hit_becomes_a_verified_book_with_pairing_fields(monkeypatch): ...
def test_eslite_is_tried_when_taaze_has_no_same_book(monkeypatch): ...
def test_main_title_is_tried_only_after_the_full_title_finds_nothing(monkeypatch): ...
def test_no_store_hit_keeps_ncl_not_found(monkeypatch): ...
def test_english_books_also_fall_back_to_bookstores(monkeypatch): ...
def test_quota_exceeded_never_reaches_bookstores(monkeypatch): ...
def test_empty_bookstore_sources_disables_the_fallback(monkeypatch): ...
def test_authorless_generic_title_with_two_same_name_candidates_is_not_verified(monkeypatch): ...   # Review Focus 2
def test_saved_pairing_fields_keep_match_auditor_from_judging_again(isolated_db): ...
    # _stage_save 寫入 source="taaze"、pairing_verdict="same" 後，match_auditor._pending_query 查不到它
```

- [ ] **Step 2: 跑測試確認失敗** → FAIL；**Step 3: 實作**；**Step 4:** 本檔、`tests/test_validator*.py`、整個 `pytest` → PASS

- [ ] **Step 5: Commit**（`feat: validator 在原有來源查不到時改查 TAAZE、誠品`）

### Task 8: 來源標籤、文件與只讀試跑

**Files:**
- Modify: `src/library_agent/trace_view.py`（「配到的書（…）」）、`src/library_agent/dashboard.py`（審核列的來源）、`CLAUDE.md`（validator 一節與「注意事項」）、`README.md`（Agent 表的 validator 一列）
- Create: `experiments/bookstore_dryrun.py`
- Test: `tests/test_trace_view.py`、`tests/test_dashboard_review.py`

**Interfaces:**
- Produces: `trace_view.SOURCE_LABEL = {"taaze": "TAAZE（書商）", "eslite": "誠品（書商）"}` 與 `source_label(code: str | None) -> str`（不在表裡的原樣回傳）；dashboard 匯入同一個函式
- Produces: `experiments/bookstore_dryrun.py [N]`：唯讀查開發庫的查無此書（`verified=False`、`review_status='pending'`、不重複書名，可限前 N 本），對每本呼叫 `_bookstore_fallback`，印出命中數（依來源）與明細，**不寫資料庫**

- [ ] **Step 1: 寫失敗的測試** — `test_trace_shows_bookstore_source_labels`（`source="taaze"` → 片段含「TAAZE（書商）」）、`test_review_row_shows_bookstore_source_label`、`test_unknown_source_code_is_shown_as_is`
- [ ] **Step 2: 跑測試確認失敗** → FAIL；**Step 3: 實作**（含 CLAUDE.md、README）；**Step 4:** 整個 `pytest` → PASS
- [ ] **Step 5: 只讀試跑** — `python experiments/bookstore_dryrun.py 30`，Expected：印出兩家各命中幾本與明細，開發庫筆數前後不變（跑前跑後各查一次 `verified_books` 的 `source` 分布，相同）
- [ ] **Step 6: Commit**（`feat: 書商來源標籤、只讀試跑腳本與文件`）

**交付時告訴使用者**：真的重跑 validator 會刪掉並重建這些書的館藏與建議，之後要接著跑 match_auditor（不會重判書商命中的書）、librarian、recommender；何時跑由使用者決定。
