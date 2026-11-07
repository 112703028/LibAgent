# 購物車決定搬移與書商後備驗證（TAAZE、誠品）設計

日期：2026-11-08


**實作順序**：先做第一部分（購物車決定搬移），再做第二部分（書商後備驗證）。
書商功能第一次跑 validator 就可能改變某些書的識別鍵，先有搬移，購物車裡的決定才不會掉。

## 第一部分（先做）：書的識別鍵改變時，購物車的決定跟著搬

### 問題

購物車的決定以 `book_key` 對應（`purchase_decisions`），鍵由書目資料算出（有效 ISBN → `isbn:<13碼>`，否則 `title:<正規化書名>`）。
鍵一變，原本的決定就找不到這本書：採購表顯示「尚未決定」、購物車與匯出少一本，**畫面上沒有任何提示**。
會讓鍵改變的情況：
1. 書商驗證成功（這次）：誠品補上 ISBN，或書名換成書商的寫法。
2. 審核時「買原書」「改買其他書名」：查無此書的書本來就在採購建議裡，可能先被加進購物車。
3. 重跑 pipeline：重新驗證後書名或 ISBN 不同（例如 `nla.py` 新匹配器），可能一次一批。

2026-11-08 查開發庫：購物車 3 筆決定中有 1 筆在「查無此書」的書上（《社區林業技術手冊》，3 冊）。
使用者決定：做搬移，而且**排在書商驗證之前**（書商功能第一次跑 validator 就會改到這本的鍵）。

### 做法

1. **決定時多記兩樣東西**：`purchase_decisions` 加兩個可為 NULL 的欄位（要一個 Alembic migration）：
   - `title`：當時這本書的書名（給人看；取該建議代表書目的 `buy_title`）。
   - `citation_ids`：這本書由哪些課綱書目組成（`recommendation_courses.citation_id`，JSON 整數陣列）。
     課綱書目的 id 不會因為重新驗證而改變（validator 重存的是 `verified_books`，citation 不動），所以可以當作「這是哪本書」的線索。
   - `/purchase/decide` 與 `/purchase/decide-many` 寫入或更新決定時一併寫入。清除決定（刪列）不用。
   - migration 用目前的採購建議補上現有決定的這兩欄；對不上任何建議的維持 NULL。
2. **每次重算採購建議後對帳**：在 `recommender.recommend()`（全校與限縮兩種都是）最後 commit 之後，呼叫一個新的對帳函式
   （例如放在新模組 `purchase_reconcile.py`）。不在 validator／librarian 之後做：它們會先刪掉建議，要等 recommender 重建完才對得起來。
   對每一筆決定：
   - **鍵還在** → 把 `citation_ids` 與 `title` 更新成這本書現在的內容（這本書之後又多了課，下次搬移的線索才完整）。
   - **鍵不在了** → 看它記下的 `citation_ids` 現在屬於哪些建議：
     - **全部指向同一本、而且那本還沒有決定** → 搬過去：改 `book_key`，冊數與 `suggested_at_decision` 照舊
       （系統建議變了的話，畫面照現有規則標出「建議已從 A 變成 B」），`title`／`citation_ids` 更新成新書的。
     - 新書**已經有自己的決定** → 不合併、不覆蓋，舊的留著成為「找不到的決定」。
     - 指向兩本以上（書被拆開）、一本也沒有（例如被退回、被配對稽核擋下）、或 `citation_ids` 是 NULL → 不猜，留著成為「找不到的決定」。
   - 對帳回傳搬了幾筆、留下幾筆，印在 pipeline 的執行紀錄。
3. **「找不到的決定」要看得到**：`GET /cart` 多回一個 `orphans` 陣列（`book_key`、`title`、`copies`；只列 `copies ≥ 1` 的）。
   購物車抽屜在清單下方顯示「找不到對應的書（N）」：每筆顯示書名與冊數，旁邊有「移除」（清除這筆決定）。
   它們**不算進**購物車書數／冊數、不匯出（書已不在採購建議裡，沒有東西可買）。決定 0（這次不買）的孤兒不顯示，靜靜保留。
4. **文件**：CLAUDE.md 「購物車與冊數決定」的「限制」改寫成這套搬移規則。

### 測試（獨立測試資料庫）

- 決定時寫入 `title`、`citation_ids`；批次決定也是。
- migration 補上現有決定的兩欄（升級前後各一份資料）。
- 對帳：鍵沒變只更新兩欄；鍵變了且只對到一本 → 搬過去（冊數、決定當時的建議都保留）；新書已有決定 → 不覆蓋；拆成兩本 → 不搬；書不見了 → 不搬；
  `citation_ids` 為 NULL → 不搬；限縮版 `recommend(only_citation_ids)` 結束時也會對帳。
- `/cart` 的 `orphans`：只列冊數 ≥ 1、不算進合計。
- 抽屜的「找不到對應的書」與「移除」：用 jsdom 驗證（看得見、移除後消失）。
- 端對端：書目改了 ISBN（模擬書商補上 ISBN）→ recommender 重算 → 決定跟著搬到新鍵、購物車冊數不變。

## 第二部分：書商後備驗證

### 目的與原因

validator 在權威書目查不到的書，會標成 `ncl_not_found`／`not_found`、`verified=False`，進審核區的「查無此書」（全庫不重複書名約 434 本，其中 388 本是中日文）。
2026-11-08 用 API Anything 試驗：從中隨機抽 30 本，人工剔除誤判後，中日文 7/20、英文 2/10 能在 TAAZE 或誠品找到同一本（約三成）。
也就是說，其中有不少是**權威書目沒收錄、書店有賣的真實書**，不應該被當成「查無此書」。

這次讓 validator 在原本的來源都查不到之後，再查 TAAZE 和誠品；**找到同一本就當作驗證成功**（使用者決定）。

### 目標

- NCL＋Google（中文）或 LOC＋Google（英文）都查不到的書，依序再查 TAAZE、誠品。
- 判斷「是不是同一本」**沿用 match_auditor 的階梯式判斷**（字串規則＋LLM），全系統只有一套標準（使用者決定）。
- 判定是同一本 → `verified=True`、`source` 記 `taaze`／`eslite`，書目資料用書商的；誠品有 ISBN 就一併記下。
- 這些書離開「查無此書」，採購表的「未驗證」標記自然消失（`_confirmed()` 看 `verified`）。
- 兩家都用 Python 寫成 `integrations/` 下的模組，與 `nla.py`、`loc.py` 同一種寫法（使用者決定，不把 API Anything 當正式依賴）。

### 非目標

- 博客來（Cloudflare 驗證擋住）、三民、金石堂（試驗沒找到結果請求）。
- 課綱錯字修正（「陳寅格」→ 陳寅恪）：書商搜尋不會自動更正，是另一個題目。
- 改 NCL 的流程（NCL 仍是「先接受、事後稽核」，見下方「與 NCL 的差別」）。
- 去 TAAZE 商品頁抓 ISBN（第一版只用搜尋結果）。
- 論文與實驗數據。

### 絕不能發生的事

- **把不同的書當成驗證成功**：寧可留在「查無此書」。
- **書商掛掉讓 pipeline 中斷**：任一家出錯都當作這家沒找到，流程照跑。
- 送任何資料到 API Anything 的共用目錄（正式版根本不用它）。
- 大量、並發地打書商：每家同一時間只有一個請求，間隔至少約 1 秒。

### 限制與前提

- 環境是 Windows；**誠品需要這台電腦已安裝的 Edge**（使用者同意）。Python 的 `playwright`（1.61）已在環境裡，用 `channel="msedge"`，**不下載瀏覽器**。要加進 `pyproject.toml` 的相依套件。
- 誠品的 JSON 接口用一般 HTTP 請求會被 Cloudflare 回 403，所以要在瀏覽器頁面裡呼叫（等同 API Anything 的 tier 2）。
- TAAZE 的伺服器 TLS 憑證鏈設定錯誤（葉憑證由 `Sectigo Public Server Authentication CA EV R36` 簽發，伺服器卻送舊的中繼憑證），httpx 預設會驗證失敗。
  解法：把 Sectigo 官方的那張中繼憑證（AIA：`http://crt.sectigo.com/SectigoPublicServerAuthenticationCAEVR36.crt`，PEM 版）放進版本庫，
  建 SSL context 時在 certifi 的根憑證之外**額外載入**它。**憑證檢查保持開啟**，不可用 `verify=False`。
- 書商的使用條款由使用者自行確認；程式端只做低頻率的讀取。

### 怎麼做（已決定的部分）

#### 1. 查詢順序與時機

在 `_validate_chinese` 與 `_validate_english` 裡，原本的來源都查不到、要回傳 not_found 之前，呼叫新的書商後備：

```
TAAZE（純 HTTP，快）→ 誠品（瀏覽器，慢）→ 都沒有才回 not_found／ncl_not_found
```

- Google 配額爆掉（`QuotaExceededError`）那條路照舊**直接回傳 quota_exceeded，不查書商**：這本書之後會被優先重驗。
- 中英文書都查（試驗中兩家都有英文書命中）。
- 現有約 430 本查無此書**不需要另外處理**：validator 每次執行都會重查 not_found 的書（`_process_one`），下次跑就會補查。

#### 2. 每一家怎麼查

每家最多兩次查詢：
1. 用**完整書名**查。
2. 第 1 次沒有被判為同一本的候選，而且「主書名」與完整書名不同時，再用**主書名**查一次。
   主書名＝第一個分隔符號前的部分（`——`、`──`、`—`、`–`、`:`、`：`、` - `、`/`、`／`），去掉頭尾空白；少於 2 個字就不補查。
   （TAAZE 的搜尋很嚴格，完整書名帶副標題常查不到，試驗中補查多找到 3 本。）

**判斷時一律拿課綱的完整書名與作者去比**，不是拿主書名比。

**TAAZE**（`integrations/taaze.py`）
- `GET https://www.taaze.tw/rwd_searchResult.html?keyType%5B%5D=0&keyword%5B%5D=<書名>`，用 BeautifulSoup 解析。
- 每一筆結果是 `div.media`：書名 `h4.media-heading a`、作者 `div.author a`、出版社 `div.publisher a`、出版日期 `div.pubDate`。
  `div.media` 一個都沒有＝查無結果（試驗中 TAAZE 對查無結果的回應就是這樣）。
- 書名去掉結尾的 `(二手書)` 這類標記；作者用 `、`、`,`、`/` 切開。
- 搜尋結果沒有 ISBN。

**誠品**（`integrations/eslite.py`）
- 接口：`GET https://holmes.eslite.com/v1/search?q=<書名>&page_size=20&page_no=1`（試驗時頁面另外帶了 `final_price=0,`、`visitor_id=…`；
  先試不帶，必要時 `visitor_id` 用隨機 hex 產生）。回應的 `results` 是陣列，每筆有 `name`、`authors`（陣列，可能是「甲/ 乙」一個字串）、
  `manufacturers`、`publish_time`、`isbn10`、`is_book`。
- 只取 `is_book == "yes"` 的結果。`isbn10` 用 `book_identity.normalize_isbn` 轉成 13 碼；轉不過（檢查碼錯）就當沒有。
- 用 Playwright 開 Edge（headless），先載入 `https://www.eslite.com/` 一次，之後在這個頁面裡用 `fetch()` 呼叫上面的接口。
- **Playwright 的同步 API 不能跨執行緒使用**，而 validator 有 2 個 worker 執行緒（`_MAX_WORKERS = 2`）：
  所有誠品查詢要交給**同一條專用執行緒**執行（例如模組內一個 `max_workers=1` 的 executor，瀏覽器在裡面延遲啟動、重複使用），
  validator 結束時關閉瀏覽器。
- Edge 不存在、Playwright 啟動失敗、Cloudflare 擋住、逾時：記一行 log，這次 validator 執行期間不再嘗試誠品（避免每本書都等逾時），回傳空結果。

**兩家共同**
- 每家各自限速：同一時間只有一個請求，兩次請求間隔約 1 秒加隨機抖動（0.7–1.3 倍）。
- 任何例外（網路、解析失敗、改版）都回傳空結果，不往外丟。
- 模組介面比照 `nla.py`：回傳候選清單（書名、作者清單、ISBN-13 或 None、出版社、出版日期、`source`）。

#### 3. 判斷是不是同一本：沿用 match_auditor

把 match_auditor 現在的階梯式判斷抽成一個公開函式（放在 `match_auditor.py`，`match_auditor_node` 與書商後備共用，行為不變）：

1. 字串規則 `rule_score(原書名, 候選書名)`：**低於 `RULE_THRESHOLD`（0.6）且不是跨文字（`_cross_script`）→ 不同本**，不打 LLM。
2. 其餘 → `_llm_same_book`（`LLM_MODEL_MINI`、temperature 0、同一份 prompt）。

書商後備的用法：
- 候選先過字串規則，依 `rule_score` 由高到低排序，跨文字的候選排在規則通過的後面。
- **最多拿前 3 名**依序問 LLM，第一個判「同一本」的就採用，其餘不再問。
- **LLM 失敗時當作不同本**（`fail_as_same=False`，與 `book_identity` 去重相同的保守方向；稽核本身仍是失敗當作同一本）。
- 每本書的上限：2 家 × 2 次查詢 × 3 次 LLM。一般情況多數書商查不到，就不打 LLM。

#### 4. 寫進資料庫的內容

判定為同一本時：

| 欄位 | 值 |
|---|---|
| `verified` | `True` |
| `source` | `taaze` 或 `eslite` |
| `canonical_title` / `canonical_authors` | 書商的書名、作者 |
| `isbn_13` | `citation.isbn or 書商的 ISBN-13`（與現有來源同一個寫法） |
| `requires_human_review` | 照現有規則（解析信心低才 True） |
| `pairing_verdict` / `pairing_judge` / `pairing_reason` | `same` / `llm` / LLM 的理由 |

- 稽核結果在驗證時就寫好，所以 `match_auditor` 的待稽核查詢（`pairing_verdict IS NULL`）會自動跳過，**同一本不會判兩次、不重複付費**。
- 需要在 `state.VerifiedBook` 加三個可選欄位（`pairing_verdict`、`pairing_judge`、`pairing_reason`，預設 None），並在 `validator._stage_save` 寫進資料庫。
  其他來源維持 None（照舊由 match_auditor 稽核）。

#### 5. 設定

`config.py` 新增 `bookstore_sources: str = "taaze,eslite"`（環境變數 `BOOKSTORE_SOURCES`）：逗號分隔、依序查；空字串＝關閉書商後備。
不想開瀏覽器時可以設成 `taaze`。

#### 6. 畫面

追溯畫面「配到的書（…）」與審核列的來源目前直接顯示代碼（`nla`、`ncl_not_found`）。
新增一個來源標籤對照，`taaze` 顯示「TAAZE（書商）」、`eslite` 顯示「誠品（書商）」；其他代碼維持原樣（不在這次範圍）。


### 與 NCL 的差別（為什麼書商要「先判斷、再接受」）

| | NCL | 書商（新） |
|---|---|---|
| 判斷標準 | match_auditor（規則＋LLM） | 同一套 |
| 判斷時機 | 先接受，事後稽核 | 接受前就判斷 |
| 候選怎麼挑 | 作者有重疊的那筆，沒有就取第一筆 | 規則相似度前 3 名，逐一給 LLM 判斷 |
| LLM 失敗時 | 當作同一本 | 當作不同本 |
| 判錯的書去哪 | 進「配錯」，要人決定 | 留在「查無此書」 |

NCL 配到的是權威書目，配錯時那筆記錄仍有參考價值（審核時可「改填配到的書名」）；書商的候選只是相關結果，配錯送進「配錯」區只會製造噪音。

### 測試（書商）

- **不連網**：兩家各存一份真實回應當測試資料（TAAZE：有結果與查無結果的搜尋頁，**裁到只剩幾筆 `div.media`**；誠品：一份 JSON，也裁到幾筆）。
- `integrations`：解析出書名／作者／ISBN、去掉「(二手書)」、作者切分、ISBN-10 轉 13 碼、查無結果回空清單、例外回空清單、TAAZE 的 SSL context 確實載入了額外憑證。
- 判斷函式：規則低分不打 LLM、跨文字交給 LLM、LLM 失敗時 `fail_as_same=False` 判不同本、只問前 3 名、`match_auditor_node` 的既有測試照樣通過。
- validator（用假的查詢與判斷函式）：原本來源都查不到 → TAAZE 命中 → 寫入 `source=taaze`、`verified=True`、稽核三欄；TAAZE 沒命中 → 試誠品；都沒有 → 維持 not_found；完整書名沒命中才用主書名補查；配額爆掉不查書商；`BOOKSTORE_SOURCES` 空字串時不查。
- 資料庫：稽核三欄寫進去後，`match_auditor` 的待稽核查詢不會再挑到它（用 `isolated_db`）。
- 畫面：來源標籤。
- 真實資料驗證：寫一支**只讀**的試跑腳本（放 `experiments/` 或暫存區），對現有查無此書跑書商後備、印出命中數與命中明細，**不寫資料庫**。
  真的重跑 validator 會刪掉並重建這些書的館藏與建議（`_stage_save`），那由使用者決定何時跑。

## 留給實作者決定

- 模組內部的函式切法、資料類別名稱、log 的措辭。
- 誠品專用執行緒的具體寫法（只要滿足「Playwright 只在一條執行緒裡用」與「validator 結束時關閉」）。
- 測試資料檔的位置與命名。

## 我（Claude）替使用者做的決定，請一併確認

1. 查詢順序是 TAAZE 再誠品；中英文書都查。
2. 每家最多兩次查詢（完整書名、主書名）；判斷一律用完整書名與作者。
3. 每次最多讓 LLM 判斷前 3 名候選；LLM 失敗當作不同本。
4. 稽核結果在驗證時寫好，match_auditor 不再重判。
5. 誠品失敗一次就在這次執行內停用誠品。
6. 新增 `BOOKSTORE_SOURCES` 設定，可關掉誠品或整個後備。
7. 第一版不抓 TAAZE 商品頁的 ISBN。
8. 新分支從 `feat/review-panel` 切出（它還沒合併進 main，畫面標籤會改到那邊的檔案）。
9. 搬移的對帳掛在 `recommender.recommend()` 結束時（全校與限縮都對帳），不掛在 validator／librarian。
10. 搬移時 `title`／`citation_ids` 更新成新書的；鍵沒變時也更新，讓線索保持完整。
11. 新書已經有決定時不合併，舊的成為「找不到的決定」讓使用者處理。
12. 「找不到的決定」只顯示冊數 ≥ 1 的，不算進購物車合計、不匯出；決定 0 的靜靜保留。
