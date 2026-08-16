# Library Agent

AI Agent 驅動的課程大綱分析與圖書館採購決策支援系統，專為國立政治大學圖書館設計。

## 系統架構

本系統以 [LangGraph](https://github.com/langchain-ai/langgraph) 建構多 Agent Pipeline，依序執行以下六個步驟：

```
crawler → parser → discoverer → validator → librarian → recommender
                                    ↓
                               human_review（有待審書目時才執行）
```

| Agent | 功能 |
|-------|------|
| **crawler** | 從 Excel 課程大綱檔讀取課程資料，存入資料庫 |
| **parser** | 用 LLM 從課綱文字解析出書目（書名、作者、ISBN 等） |
| **discoverer** | 對完全沒有書目的課程，用 LLM 根據課綱內容推薦書目 |
| **validator** | 透過 LOC、Google Books、Alma SRU 驗證書目真實性，補齊 ISBN |
| **librarian** | 查詢 Alma 館藏，確認每本書的館藏狀態與冊數 |
| **recommender** | 用 LLM 根據館藏狀態、選課人數、書目類型，決定採購優先級與建議冊數 |

## 環境需求

- Python 3.11+
- Docker（執行 PostgreSQL）
- OpenAI API Key
- Alma API Key

## 安裝

```bash
# 安裝相依套件
pip install -e .

# 複製環境變數範本
cp .env.example .env
# 編輯 .env，填入 API Keys
```

### `.env` 設定

```env
OPENAI_API_KEY=sk-...
ALMA_API_KEY=...
LLM_MODEL=gpt-4o-mini
```

## 啟動資料庫

```bash
# 啟動 PostgreSQL（每次開機後需要執行）
docker-compose up -d

# 第一次使用，建立資料表
alembic upgrade head
```

## 執行

```bash
# 執行完整 pipeline 並輸出採購報表
library-agent run

# 測試用：只跑前 10 堂課
library-agent run --limit 10

# 只輸出報表（不重跑 pipeline，需先有資料）
library-agent report

# 輸出報表並存成 CSV
library-agent report --output report.csv
```

也可以單獨執行各個 Agent：

```bash
python src/library_agent/agents/crawler.py
python src/library_agent/agents/parser.py
python src/library_agent/agents/parser.py 20    # 測試用，只跑前 20 筆
python src/library_agent/agents/discoverer.py
python src/library_agent/agents/validator.py
python src/library_agent/agents/librarian.py
python src/library_agent/agents/recommender.py
```

## 採購優先級說明

| 優先級 | 說明 |
|--------|------|
| **HIGH** | 指定用書，館藏不存在或僅有書目記錄但無實體 |
| **MEDIUM** | 選用書，館藏不存在或僅有書目記錄但無實體 |
| **LOW** | 已有實體館藏，但冊數不足 |
| **SKIP** | 已有電子書，或館藏數量充足 |

## 資料庫管理

```bash
# 查看各 table 筆數
python -c "
from sqlalchemy import select, func
from library_agent.db.models import Course, Citation, VerifiedBook, HoldingCheck, Recommendation
from library_agent.db.session import SessionLocal
with SessionLocal() as s:
    for model in [Course, Citation, VerifiedBook, HoldingCheck, Recommendation]:
        n = s.scalar(select(func.count()).select_from(model))
        print(f'{model.__tablename__}: {n}')
"

# 建立新的 migration（schema 有變更時）
alembic revision --autogenerate -m "描述"
alembic upgrade head
```

## 注意事項

- `docker-compose down` 資料保留；`docker-compose down -v` **資料全刪**，慎用
- Google Books API 每日配額 1000 次；中文書改走 Alma SRU，不佔配額
- LOC（美國國會圖書館）為英文書首選驗證來源，無配額限制
- PostgreSQL 連接埠綁定在 `127.0.0.1:5432`，防止外部存取
