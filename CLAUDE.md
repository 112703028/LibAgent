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
  待審核列（還沒有採購建議）也有 ▸ 鈕，改取 `GET /trace/pending/{verified_book_id}`：只有課綱、解析、配對、館藏，
  第 ⑤ 段改說明「為什麼還沒進採購建議」（配錯／驗證階段查無此書／解析信心低），標題用原書名而非配到的書。
  查無此書的（實測 513/532 筆「未稽核」都是）右側顯示「查無記錄」，館藏是用原書名直接查 Alma 的結果。
- **冊數算式是重算的**：資料庫只存結果，畫面用同一套 `recommender.merge_group` 與 `recommendation_courses.enrolled`
  （計算當時的人數）重算；與資料庫存的建議不同時（上限、館藏或資料在建議產生後改過）會標出 `.tr-drift` 差異。
- **課綱截斷**：最多顯示 `MAX_SYLLABUS`（6000）字，並標明原長度（最長的課綱有 32454 字）。
- 展開鈕內不得放文字（匯出 CSV 讀 `textContent`）；展開列 `tr.trace-row` 不是資料列，篩選、搜尋、全選、匯出都要略過它。

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
