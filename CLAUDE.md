# Library Agent

AI Agent 驅動的課程大綱分析與圖書館採購決策支援系統（政大）。

## 環境啟動

```bash
# 啟動 PostgreSQL（每次開機需要）
docker-compose up -d

# 確認容器狀態
docker ps
```

## LLM 供應商 / 模型

所有 LLM 呼叫走 **OpenAI 相容**介面，供應商與模型由 `.env` 設定（**金鑰只放 `.env`，勿寫進本檔或 commit**）：

```bash
# .env
OPENAI_API_KEY=<你的金鑰>
OPENAI_BASE_URL=https://www.myai168.com/tw/api/openai/v1   # 目前用 myai168 閘道；留空=OpenAI 官方
LLM_MODEL=gpt-5.5            # 主模型（parser 解析、discoverer 探索）
LLM_MODEL_MINI=gpt-5.4-mini # 輕量呼叫（parser 判斷是否抓 PDF、recommender 採購決議）
```

- 查可用模型：`GET {OPENAI_BASE_URL}/models`（或 openai SDK `client.models.list()`）。
- **注意（myai168）**：沒有 `gpt-4o`/`gpt-4o-mini`；新的 `gpt-5.5`/`gpt-5.6` 是 reasoning 型、**只接受 `temperature=1`**。recommender 用 `temperature=0`，故 `LLM_MODEL_MINI` 必須選支援 temp=0 的模型（如 `gpt-5.4-mini`）。

## Pipeline 指令（依序執行）

```bash
# 1. 爬取課程大綱資料
python src/library_agent/agents/crawler.py

# 2. 解析書目（全部）
python src/library_agent/agents/parser.py

# 2. 解析書目（測試用，只跑前 N 筆）
python src/library_agent/agents/parser.py 20

# 3. 驗證書目（英文 → LOC / Google Books，中文 → NCL/NBINet SRU）
python src/library_agent/agents/validator.py

# 4. 稽核配對：確認驗證器配到的記錄真的是原書（配錯 → 送人工審核並擋下採購）
python src/library_agent/agents/match_auditor.py
python src/library_agent/agents/match_auditor.py 20   # 測試用，只跑前 N 筆

# 5. 查詢館藏狀態（Alma SRU）
python src/library_agent/agents/librarian.py
```

### 書商後備驗證（validator 查不到時）

NCL／LOC／Google 都查不到的書，validator 依 `BOOKSTORE_SOURCES`（預設 `sanmin,taaze`，逗號分隔、依序查，空字串＝關閉）再查書商。
`_bookstore_fallback`（`agents/validator.py`）：每家先用完整書名，沒有採用的候選且有主書名（`bookstore.main_title`）時再用主書名；
判斷一律拿課綱的完整書名與作者，用 match_auditor 的 `pick_same_book`（字串規則＋LLM，最多問前 3 名，**LLM 失敗＝不同本**）。
判定同一本就是驗證成功：`source` 記 `taaze`／`eslite`、`verified=True`，並把稽核結果寫進 `pairing_verdict/judge/reason`
（match_auditor 之後不會再判）。課綱沒寫作者、書商有兩筆以上同名候選時不採用；Google 配額爆掉的書不查書商；書商出任何錯都當作沒找到。

- **順序為什麼是三民先**（2026-11-08 隨機 100 本「查無此書」、`experiments/bookstore_compare.py`）：三民完整跑完命中 36%、0 次 429；TAAZE 回過 429 後冷卻、其餘書被跳過，所以它的 5% 不可信。三民先查，TAAZE 只需要查剩下的約六成，請求量少三分之一，也比較不會被限流。
  命中裡約 8 本是寬鬆判斷（第 2 冊 vs 第 1 冊、英文版、簡體版、合訂本配單冊）：書存在，但不一定是要買的那一本，`pairing_reason` 看得到理由。
- **TAAZE**（`integrations/taaze.py`）：純 HTTP，解析搜尋頁的 `div.media`（沒有 ISBN）。伺服器的 TLS 憑證鏈設定錯誤，
  在 certifi 之外額外載入 `integrations/certs/` 裡 Sectigo 官方的中繼憑證，**憑證檢查保持開啟**。每次請求間隔約 2 秒；
  回 **429 就依 `Retry-After`（最多 30 分鐘，沒有就 5 分鐘）冷卻**（共用的 `bookstore.Cooldown`），冷卻期間不送請求。
- **三民**（`integrations/sanmin.py`）：純 HTTP，`GET /search/?ct=k&qu=關鍵字`（`ct=k` 是關鍵字搜尋），解析 `div.Prod`（沒有 ISBN），只取前 20 筆。
  **三民是模糊比對、按相關性排序：搜不存在的書名也會回字面上有點像的不相關書**，所以一定靠 `pick_same_book` 判斷，不能因為「有結果」就算找到。
  請求用專案自己的 User-Agent（不偽裝成瀏覽器），間隔約 2 秒，同樣有 429 冷卻。試驗時工具學到的 `?ct=all&qu=`、`?all=` 都不是正確的組合。
- **誠品**（`integrations/eslite.py`）：Playwright 驅動這台電腦的 Edge、在頁面裡呼叫 JSON 接口，查詢集中在專用執行緒，失敗一次就在這次執行內停用。
  **2026-11-08 實測：誠品的 Cloudflare 對誠實的無頭 Edge 回 403，所以目前查不到東西，預設不啟用。**
  API Anything 能通是因為它把 User-Agent 的 `HeadlessChrome` 改寫成 `Chrome`、讓網站認不出是無頭瀏覽器，那是繞過存取限制，**不要照做**。
- 現有的查無此書不用另外處理：validator 每次執行都會重查 not_found 的書。真的重跑會刪掉並重建這些書的館藏與建議
  （`_stage_save`），之後要接著跑 match_auditor（不會重判書商命中的書）、librarian、recommender。
- 只讀試跑：`python experiments/bookstore_dryrun.py 30`（呼叫書商與 LLM，不寫資料庫）。
- 畫面上來源顯示成「TAAZE（書商）」「三民（書商）」「誠品（書商）」（`trace_view.source_label`）。

### 配對稽核（match_auditor）

validator 只要查得到記錄就算 `verified=True`，實測有相當比例配到的是別的書
（最常見：書名相同但作者不同的教科書，如不同作者的 *Macroeconomics*）。
階梯式判斷，門檻與方法都在 300 筆人工標註上比較過（`experiments/same_book_judge_compare.py`）：

1. 字串規則（書名正規化後比 bigram Jaccard，門檻 0.6）判「不同本」→ 直接標記，省下 LLM 呼叫
2. 規則判「同一本」→ 問 `LLM_MODEL_MINI` 複核（書名相同但作者不同只有這層抓得到）

結果寫在 `verified_books.pairing_verdict / pairing_judge / pairing_reason`；
`pairing_verdict IS NULL` 代表還沒稽核，重跑只處理這些列，不會重複付 LLM 費用。
判為 `mismatch` 的書會進人工審核佇列，並被 `db/filters.py` 的 `purchasable()`
擋在採購建議/報表/dashboard 之外，直到人工改成 `approved`。

### 採購建議：一本書一筆（recommender）

同一本書被好幾門課用到時，只產生**一筆**建議：人數是各課加總（同一門課重複列只算一次）、現有館藏**只扣一次**、
任一課指定就按指定算。`_decide` 的規則完全沒變，只是輸入從「單一課」變成「合計」。

- **怎麼認定同一本書**（`book_identity.py`）：過檢查碼的 ISBN（ISBN-10 轉 13 碼）→ `isbn:<13碼>`；
  沒有有效 ISBN → 依正規化書名分桶，桶內記錄不完全相同時用 match_auditor 的判斷（失敗時**不合併**）。
  不同的有效 ISBN 不合併（版次分開）。`isbn_13` 欄位不一定是 ISBN（有 Alma 記錄編號），所以一定要先過檢查碼。
- **資料表**：`recommendations` 一列 = 一本書（`book_key` 唯一；`course_id` / `holding_id` 是代表課程 / 代表館藏）；
  `recommendation_courses` 記錄這本書被哪些課用到。外鍵 `ON DELETE CASCADE`：validator / librarian 會直接
  `DELETE recommendations`，parser 會先刪舊 citations，沒有 cascade 這些節點會被外鍵擋住。
- **recommender 永遠算全校**，不看 `course_ids`（只算選中的課，合計會失真）。冪等簽章沒變就不重打 LLM。
- **重建**：`python src/library_agent/agents/recommender.py`。`recommendations` 完全是推導資料，隨時可重建；
  首次（或換識別規則後）要重寫每本書的理由，約 1500 次 LLM。
- **測試**：recommender 會清掉不再存在的建議，所以它的測試用獨立的測試資料庫（`conftest.py` 的 `isolated_db`，
  `<開發庫>_test`），**不要**在開發庫上跑會呼叫 `recommender_node` 的測試。

### 單筆追溯（dashboard 明細表每列的 ▸ 鈕）

點採購建議那一列的 ▸，在下方展開這本書的決策過程：課綱原文 → 解析結果 → 驗證配對（原書 vs 配到的書並排＋
match_auditor 判定）→ Alma 館藏 → 冊數怎麼算。**只讀**，不核准也不退回（放行仍用 `main review`）。

- **端點**：`GET /trace/{id}` 回已逸出的 HTML 片段（`trace_view.py`）；前端第一次展開才取，之後只切換顯示。
  待審核的書（還沒有採購建議）在「人工審核」區塊（見下節）有 ▸ 鈕，改取 `GET /trace/pending?ids=…`（一組可含多個書目，
  至多 50 個）：只有課綱、解析、配對、館藏，
  第 ⑤ 段改說明「為什麼還沒進採購建議」（配錯／驗證階段查無此書／解析信心低），標題用原書名而非配到的書。
  查無此書的（實測 532 筆「未稽核」全是這類）右側顯示「查無記錄」，館藏是用原書名直接查 Alma 的結果。
- **冊數算式是重算的**：資料庫只存結果，畫面用同一套 `recommender.merge_group` 與 `recommendation_courses.enrolled`
  （計算當時的人數）重算；與資料庫存的建議不同時（上限、館藏或資料在建議產生後改過）會標出 `.tr-drift` 差異。
- **課綱截斷**：最多顯示 `MAX_SYLLABUS`（6000）字，並標明原長度（最長的課綱有 32454 字）。
- 展開鈕內不得放文字（匯出 CSV 讀 `textContent`）；展開列 `tr.trace-row` 不是資料列，篩選、搜尋、全選、匯出都要略過它。

### 人工審核（dashboard：與採購建議同一張卡片的分頁）

`review_status='pending'` 的書（實測 1280 筆，**核准與退回都是 0，從沒人處理過**）原本混在採購表裡。現在採購建議與審核在**同一張卡片**
（`#review`），**兩層導覽**：第一層 [採購｜審核]（審核是修資料、採購是做決定，兩件事分開），採購區只有採購建議一張表，
審核區有子分頁 [配錯｜查無此書｜其他｜審核紀錄]（見下節「兩層導覽」）；預設開在採購。審核分頁的每一列＝**一本書**（同一本書被多門課列出會合併，實際要做的決定約 963 個而不是 1280 個）：

- **配錯**（746 筆 → 537 組）：match_auditor 判配錯，已被 `purchasable()` 擋在採購建議之外。
- **查無此書**（532 筆 → 426 組）：validator 在權威書目查無（`verified=False`），**仍在採購建議裡**
  （426 本建議、992 冊、262 本是 HIGH）；採購表的這些書名後有「未驗證」標記（CSS 畫的，不含文字，匯出 CSV 才不會污染）。
- **其他**（2 筆）。
- 分組規則在 `review_groups.py`（純函式）：配錯依（原書名, 配到的書名），**配到的書名必須在鍵裡**，否則同名不同作者的書會被併成一組；
  查無此書只依原書名；同一門課在組內只算一次人數。學系篩選只影響組內計入哪些書目，「所在採購建議」是全校的。
- 採購表不再有待審列，優先級篩選也沒有「待審核」；KPI「待人工審核」卡片點下去會開啟待審最多的分頁
  （卡片的 `data-review-open`）；待審清單的 CSV 匯出在分頁列右側（`/export/pending`）。
### 購物車與冊數決定

系統建議只是參考；**明確加入購物車才算要買**，購物車的匯出才是最後要下單的清單。`purchase_decisions` 的一列就是購物車的內容：

| 狀態 | 資料 | 在購物車裡？ | 最終冊數 `final` |
|---|---|---|---|
| 沒決定（沒有列） | — | **否** | 0 |
| 決定冊數 N（N ≥ 1） | `copies = N` | **是** | N |
| 這次不買 | `copies = 0` | 否（明確排除，與審核的「退回」不同） | 0 |

- **採購冊數欄（最右）**：決定冊數輸入框（留空＝清除、填 0＝這次不買、填數字＝加入）與「加入購物車／移出購物車」按鈕；
  它左邊的「系統建議」欄只是參考，兩欄分開。按「加入」時：**輸入框裡有 ≥1 的冊數就用那個數字**，沒有才採用**伺服器端當下的**
  系統建議（`mode="suggested"`）；系統建議為 0 的書不能用按鈕加入（停用，要自己填冊數）。舊格式的建議（`book_key` 為 NULL）沒有輸入框與按鈕。
- **同一列的動作要排隊**（前端 `_withRow`）：輸入冊數後去按按鈕，瀏覽器會先觸發輸入框的 `change`、緊接著才是 `click`，
  兩個請求同時在飛，後到的會蓋掉先到的（曾經造成「填 12，購物車卻變成系統建議 5」）。排隊後，按鈕看到的是前一個動作存好的結果。
- **購物車不是分頁，是頁首左上角的購物車圖示**（`#cartbtn`，右上角的數字＝全校購物車的書數），點開是左側抽屜（`#cartdrawer`）：
  整個購物車、**全校、不隨學系篩選**，資料來自 `GET /cart`（打開時才取；只列「現在還在採購建議裡」的書，與採購表同一個
  `purchasable()` 條件，所以對不上任何建議的舊決定不列也不算）。抽屜裡每列可改冊數（0＝這次不買、會離開購物車）、移出，
  底部有合計、「匯出購物車 CSV」（檔名 `cart_`，欄位：書名、採購冊數、系統建議、優先級、館藏狀態）與「清空購物車」（要確認；
  清空＝清除決定，書回到「尚未決定」）。抽屜裡的變更沿用 `/purchase/*`，並同步回採購表上出現的列、KPI 與圖示。
  KPI（「購物車冊數」`copies_final`、「購物車書數」`cart_books`、「系統建議冊數」`copies`）仍是**頁面上這批書**（會隨學系篩選）的合計，
  由 DOM 即時算；圖示與抽屜是全校的。書名一律用 `textContent` 放進抽屜（不用 `innerHTML`）。
- **端點**（JSON 的 POST，驗證失敗回 400 且不寫入）：`/purchase/decide`（單本，`{book_key, copies|null}`）、
  `/purchase/decide-many`（批次，`{book_keys, mode}`，mode＝`suggested` 加入／`zero` 不買／`clear` 清除，一次最多 200 本，
  回傳 `results` 供前端就地更新；找不到建議或建議為 0 的 key 略過）。
- **存在獨立的 `purchase_decisions`（以 `book_key` 為鍵）**，不放在 `recommendations` 上：librarian／recommender 會刪掉重建建議，決定不能跟著消失
  （有測試）。另存「決定當時的系統建議」，之後系統建議變了就在該格標「建議已從 A 變成 B」。
  **書的識別鍵改變時，決定跟著搬**（`purchase_reconcile.py`）：決定時另記 `title` 與 `citation_ids`（這本書由哪些課綱書目組成；citation 在重新驗證時不變）。`recommender.recommend()`（全校與限縮）結束時對帳：鍵還在→更新快照；鍵不在→記下的書目中仍屬於某筆建議者全部指向同一本、且那本還沒有決定→搬過去（冊數、決定當時的建議、決定時間不動）；新書已有決定、拆成兩本、書不見了、沒有快照→不動，成為「找不到的決定」：`/cart` 的 `orphans`（只列冊數 ≥ 1），抽屜清單下方「找不到對應的書（N）」可移除，不算進合計、不匯出。決定時間新的先處理。不記是誰決定，只記時間。
- **「需求 N」**：被單本上限（`max_purchase_copies`，預設 5）截斷的書，冊數旁標「（需求 26）」。需求是**讀取時**用 `demand.py` 算的，不存：
  `demand.need_copies / uncapped_copies` 是唯一一份需求算式，recommender 的 `_decide`、追溯畫面、儀表板都用它，
  並有測試鎖住 `min(uncapped, 上限)` 等於 `_decide` 的冊數。
- **匯出**：見下節「統一的分頁操作」。採購建議的 CSV 與 `/export/recommendations`、`library-agent report` 都有
  「需求冊數／決定冊數／最終採購數」，**最終採購數對沒決定的書是 0**。
- 寫入決定的測試一律用 `isolated_db`，**不要**對開發庫做真實決定；真實資料庫裡可能已有使用者自己填的決定，測試不可假設它是空的。

### 兩層導覽（採購｜審核）

- **第一層**：`採購 N 本`、`審核 M 組待處理`（M＝三個待審分類的組數，取代原本 KPI 的跳轉）。**採購區只有採購建議**；
  審核區有四個子分頁與「套用並重算採購建議」橫幅。**審核沒有任何待處理與紀錄時不放第一層**（只剩採購建議），
  這時橫幅（待套用數是全校的，學系篩選時紀錄可能是空的）直接顯示、不藏。
- 屬於某一層的元素用 `data-top="buy|review"` 標記（子分頁按鈕、套用橫幅、待套用提示），不在預設那層的由伺服器先加 `hidden`
  （沒有 JS 也是對的）；子分頁列只在審核區顯示（採購區只有一個分頁，不重複）。
- **採購區頂端的提示**「有 N 筆審核決定尚未套用 → 前往審核」：套用的按鈕只在審核區，這裡只指路。
- 切第一層由 `_selectTop`，**任何分頁被選到都會經 `_selectTab` 同步第一層**（所以重新整理後還原分頁、KPI「待人工審核」連結都自然正確）；
  回到審核區會開上次用的子分頁（`_lastReview`），沒有才開待審最多的。
- **用詞**：畫面上「採購建議」＝系統算出來的那份清單（`recommendations`）；「購物車」＝使用者明確決定要買的（`purchase_decisions`）。
  **不要再用「採購單」或「採購清單」**，兩個都會和上面兩個詞搞混（審核列的按鈕是「加入採購建議」，採購表與抽屜的是「加入購物車／移出購物車」）。

### 介面樣式（`dashboard_css.py`）

樣式表在獨立模組 `dashboard_css.py` 的 `CSS`（一般字串，不用在 f-string 裡把大括號寫兩遍），由 `dashboard.py` 放進 `<style>{CSS}</style>`。
2026-11-08 用 frontend-design 重新設計，原則：

- **採購表是主角**：頁首只有一行頂欄（購物車、標題、學系）、一行管線狀態、一行帳本式數字、兩條書脊比例帶，然後就是工作區。
  管線的控制與終端機輸出收進 `<details id="pipeline">`，**平常收合，執行時（`/status` 回 running）由 JS 自動展開**；閒置時狀態列不顯示數字（和帳本重複）。
- **優先級是「書脊」**：每一列最左邊一條彩色竖條（`tr[data-priority]` 第一格的 `box-shadow: inset`；高＝氧化紅、中＝赭黃、低＝書架綠、略過＝石灰），
  取代每列一顆藥丸。**顏色不是唯一資訊**：旁邊的格子仍寫著「高／中／低／略過」。畫面用短標籤（`_PRIORITY_SHORT`），
  **匯出（CSV）仍用完整標籤「HIGH 高」**（`_PRIORITY_LABEL`，匯出時由 `tr.dataset.priority` 對應，不讀畫面上的短標籤）。
- **藥丸改用類別上色**（`pill p-high`、`pill s-missing`），不寫行內 `style="background:…"`，顏色只在 CSS 一處。
- **不要加回**：一堆圓角白卡片（整個工作區是一張紙、用細線分段）、大數字配小標籤的 KPI 卡（現在是一行內聯數字）、
  全大寫標籤、小標題（eyebrow）、用中點「·」串起來的資訊、「→」結尾的按鈕。
- 長文字（理由、課程）用 `.clamp` 截成兩、三行，完整文字放在 `title`；採購冊數欄的輸入框與按鈕同一行（列高約 58 px）。
- 工具列吸頂，表頭貼在它下面：高度由 JS 量出來放進 CSS 變數 `--tbh`（`_syncToolbarHeight`）。<900 px 時表格自己橫向捲動、頁面不橫向捲。
- 字型用系統中文字（Noto Sans TC／微軟正黑體／苹方），**不載入網路字型**（館內離線也要能用）。
- **看版面要用真瀏覽器**：`library-agent dashboard --port 8765` 後用 Playwright 開 Edge（`channel="msedge"`）截圖，只點分頁、展開、開抽屜，
  **不要點會寫入的按鈕**（開發庫是真資料）。近 1900 列的頁面做**整頁截圖會逾時**，要捲到位置只截視窗那一屏。

### 統一的分頁操作（採購建議｜配錯｜查無此書｜其他｜審核紀錄）

每個分頁都是同一種模式：**共用工具列 → 表（勾選｜優先級｜課程｜▸書名｜…｜處理）→ 勾選後的批次列**。

- **工具列只有一條**（`#toolbar`：搜尋、優先級／類別篩選、匯出），對**目前開著的分頁**生效；面板用 `data-filters`（`prio cat`／`prio`／空）宣告
  適用的篩選、`data-export`（`recs`／`review`／`log`）宣告匯出的欄位。切換分頁時搜尋、篩選與勾選都重設。
  搜尋比對列上的 `data-search`（伺服器組好的小寫文字，不依欄位位置）；搜尋框有字時只看搜尋（無視按鈕）。
- **批次列**每個面板一條：採購建議（加入購物車／設為不買／清除決定）、審核紀錄（撤銷所選）。**審核的三個待處理分頁（配錯／查無此書／其他）沒有批次列**（先拿掉，三個分頁各一套按鈕曾讓人混淆；列上仍有勾選，匯出「只匯出勾選的列」要用）。
  批次與全選只處理**看得見**的列；一律先跳確認視窗；沒有「全部改成 N 本」。
- **匯出各分頁的欄位明確組出**（不讀整列 `textContent`）：輸入框的值與「需求」標記都不是文字節點；購物車的 CSV 由抽屜匯出（只含加入的書、檔名 `cart_`）。
- **展開鈕的事件只在卡片 `#review` 綁一次**：採購表在卡片裡面，兩邊都綁會讓一次點擊觸發兩次（開了又關）。
- 審核決定、撤銷等需要整頁重新整理的動作，重新整理前把分頁與 `scrollY` 存進 `sessionStorage`，載入後還原（不設 `#review`，那會跳回卡片頂端）；
  購物車／冊數的變更則是就地更新，不重新整理。
- JS 在版本庫裡沒有自動化測試；改前端時用 jsdom 驗證並**檢查「看得見」**（`hidden`、`display`），不只是檢查元素有沒有被建立。

#### 審核動作（審核卡片上的按鈕，也是 `library-agent review` 的核准／退回走的同一份邏輯：`review_actions.py`）

三個待處理分頁（配錯／查無此書／其他）**每一列完全一樣**：

```
[書名（預填原書名）] [ISBN（選填）] [加入採購建議] [改填配到的書名]* [都不買]      * 只有配到書的列（配錯、其他）才有
```

| 按鈕 | 送出 | 結果 |
|---|---|---|
| 加入採購建議 | `custom` ＋輸入框的書名與 ISBN | approved(custom)；**輸入的書名等於這筆的原書名、又沒填 ISBN 時，後端視同 approved(original)**（保留原作者與有效的原 ISBN；逐筆比對各自的原書名） |
| 改填配到的書名 | 不送出 | 只把配到的書名填進輸入框，使用者看過再按加入——所有決定都走同一條路（輸入框的內容就是要買的書） |
| 都不買 | `none` | rejected(none) |

- `matched`（買配對到的書）與 `original` 仍是 `review_actions.decide` 的合法動作（命令列 `library-agent review` 的核准就是 `matched`，
  對查無此書的書也是），但儀表板的按鈕不再送出它們。後端**沒有**限制 `matched` 只能用在有配到書的列，是因為命令列刻意這麼用。
- 一次作用在一組的所有書目；只處理 `pending` 的書目，已被別人處理的不覆蓋（回報 `skipped`）。
  「審核紀錄」分頁（原「已決定」）可撤銷（也可批次，有確認視窗，一次請求最多 200 個 id、前端自動分批），也能勾選與展開看決策過程
  （改回待審；原本是買原書／改買其他書名的，一併刪掉用採購書名查的舊館藏與建議）。
- **採購書名**：`verified_books.purchase_title / purchase_authors / purchase_isbn`，**不覆蓋** `canonical_*`（驗證器配到的證據）。
  下游一律讀 model 的 `buy_title / buy_authors / buy_isbn`（hybrid 屬性）；**`buy_isbn` 不可 coalesce**：買原書沒有有效 ISBN 時，
  不能沿用配到那一本的 ISBN（那正是配錯的來源）。決定另記 `review_choice / reviewed_at / review_applied_at`。
- **決定要「套用」才反映到採購建議**：卡片上的「N 筆決定待套用 ［套用並重算採購建議］」→ `POST /review/apply`
  （`review_apply.apply_review`）：有採購書名或沒有館藏列者用採購書名／ISBN 重查 Alma（`librarian.recheck_holdings`），
  再由 recommender 重算全校，只有確實出現在採購建議裡、且重查沒失敗的才標 `review_applied_at`；失敗下次可重試。
  與 `/run` 共用鎖與狀態（同時只能跑一個），結束後頁面自動重新整理。退回在讀取端立刻生效（`purchasable()`）。
- 端點都是 JSON 的 POST：`/review/decide`、`/review/undo`、`/review/apply`；驗證失敗回 400 且不寫入。
  validator 本來就不重驗 approved／rejected 的書，所以決定不會被重跑 pipeline 洗掉（有測試釘住）。
- **限制**：儀表板沒有登入機制、預設只綁 `127.0.0.1`，開放區網前要另外做登入；沒有記錄是誰決定的（只記時間）。
  「買原書」沒有 ISBN 時只能用書名查 Alma，Alma 查詢不看作者，館藏可能混到同名他書。
- 寫入決定的測試一律用 `isolated_db`，**不要**對開發庫做真實決定。

## 資料庫

```bash
# 執行 migration（第一次 / schema 有變更時）
alembic upgrade head

# 建立新的 migration
alembic revision --autogenerate -m "描述"

# 查看目前 DB 內容
docker exec library_agent-postgres-1 psql -U postgres -d library_agent -c "\dt"

# 查各 table 筆數
python -c "
from sqlalchemy import select, func
from library_agent.db.models import Course, Citation, VerifiedBook, HoldingCheck
from library_agent.db.session import SessionLocal
with SessionLocal() as s:
    for model in [Course, Citation, VerifiedBook, HoldingCheck]:
        n = s.scalar(select(func.count()).select_from(model))
        print(f'{model.__tablename__}: {n}')
"

# 清除驗證失敗的書目（重跑 validator 前）
python -c "
from sqlalchemy import delete
from library_agent.db.models import VerifiedBook
from library_agent.db.session import SessionLocal
with SessionLocal() as s:
    s.execute(delete(VerifiedBook).where(VerifiedBook.verified == False))
    s.commit()
    print('done')
"
```

## 整合測試

```bash
# 測試 Google Books API
python src/library_agent/integrations/google_books.py

# 測試 Alma SRU
python src/library_agent/integrations/alma.py

# 測試 validator（只跑 10 筆未驗證書目）
python src/library_agent/agents/validator.py

# 測試 librarian（全部 verified_books）
python src/library_agent/agents/librarian.py

# 單元測試（純函式，不需 DB/網路/API key）
pytest
```

## 注意事項

- `docker-compose down` → 資料保留（volume `pgdata` 不刪除）
- `docker-compose down -v` → **資料全刪**，慎用
- Google Books API 每日配額 1000 次，中文書走 NCL/NBINet 不佔配額
- validator 的 `__main__` 預設只跑 10 筆未驗證書目（測試用）；`validator_node()` 跑全部
