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

# 4. 查詢館藏狀態（Alma SRU）
python src/library_agent/agents/librarian.py
```

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
