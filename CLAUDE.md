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

`review_status='pending'` 的書（實測 1280 筆，**核准與退回都是 0，從沒人處理過**）原本混在採購表裡。現在採購建議與審核是**同一張卡片**
（`#review`）的分頁：[採購建議｜配錯｜查無此書｜其他｜已決定]，預設開在採購建議；審核分頁的每一列＝**一本書**（同一本書被多門課列出會合併，實際要做的決定約 963 個而不是 1280 個）：

- **配錯**（746 筆 → 537 組）：match_auditor 判配錯，已被 `purchasable()` 擋在採購清單之外。
- **查無此書**（532 筆 → 426 組）：validator 在權威書目查無（`verified=False`），**仍在採購清單裡**
  （426 本建議、992 冊、262 本是 HIGH）；採購表的這些書名後有「未驗證」標記（CSS 畫的，不含文字，匯出 CSV 才不會污染）。
- **其他**（2 筆）。
- 分組規則在 `review_groups.py`（純函式）：配錯依（原書名, 配到的書名），**配到的書名必須在鍵裡**，否則同名不同作者的書會被併成一組；
  查無此書只依原書名；同一門課在組內只算一次人數。學系篩選只影響組內計入哪些書目，「所在採購建議」是全校的。
- 採購表不再有待審列，優先級篩選也沒有「待審核」；KPI「待人工審核」卡片點下去會開啟待審最多的分頁
  （卡片的 `data-review-open`）；待審清單的 CSV 匯出在分頁列右側（`/export/pending`）。
- **還沒做**（設計在 `docs/superpowers/specs/2026-10-31-review-panel-design.md`）：「採購冊數由使用者決定」。

#### 審核動作（審核卡片上的按鈕，也是 `library-agent review` 的核准／退回走的同一份邏輯：`review_actions.py`）

| 類別 | 按鈕 | 結果 |
|---|---|---|
| 配錯 | 買配對到的書／買原書／都不買 | approved(matched)／approved(original)／rejected(none) |
| 查無此書 | 書名輸入框（預填原書名）＋選填 ISBN → 加入採購清單；捨棄 | approved(custom)／rejected(none) |
| 其他 | 買這本書／都不買 | approved(matched)／rejected(none) |

- 一次作用在一組的所有書目；只處理 `pending` 的書目，已被別人處理的不覆蓋（回報 `skipped`）。多選批次只開放「買配對到的書」與「都不買」，有確認視窗，一次請求最多 200 個 id（前端自動分批）。
  「已決定」分頁可撤銷（改回待審；原本是買原書／輸入書名的，一併刪掉用採購書名查的舊館藏與建議）。
- **採購書名**：`verified_books.purchase_title / purchase_authors / purchase_isbn`，**不覆蓋** `canonical_*`（驗證器配到的證據）。
  下游一律讀 model 的 `buy_title / buy_authors / buy_isbn`（hybrid 屬性）；**`buy_isbn` 不可 coalesce**：買原書沒有有效 ISBN 時，
  不能沿用配到那一本的 ISBN（那正是配錯的來源）。決定另記 `review_choice / reviewed_at / review_applied_at`。
- **決定要「套用」才反映到採購清單**：卡片上的「N 筆決定待套用 ［套用並重算採購建議］」→ `POST /review/apply`
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
