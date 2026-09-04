# Pipeline 即時進度流程圖 — 設計文件

## 背景與目標

Dashboard（[dashboard.py](../../../src/library_agent/dashboard.py)）目前的 `/status` 只能回報 pipeline 整體狀態（idle/running/done/error）與各表筆數，無法回答「現在跑到哪一個節點」。使用者希望在前端看到類似 LangGraph 的流程圖，並即時顯示目前執行到哪一步。

Pipeline 定義在 [graph.py](../../../src/library_agent/graph.py)，共 7 個節點：

```
crawler → parser → discoverer → validator ─┬─→ librarian ─→ recommender → END
                                            └─→ human_review ──────────→ END
```

`validator` 之後 `librarian` 與 `human_review` 平行執行。

## 範圍

- 新增節點層級的執行狀態記錄（pending/running/done/error），粒度為「整個節點」，不細到節點內部單筆進度（validator/librarian 內部用 ThreadPoolExecutor 併發處理數百筆，不適合逐筆回報）。
- 前端用 Mermaid.js 畫出上述流程圖，依節點狀態即時上色。
- 更新機制沿用現有輪詢架構（前端定時打 `/status`），**不使用 WebSocket**（已評估 thread↔asyncio 橋接複雜度後放棄）。

## 架構

### 1. DB 新表 `pipeline_node_runs`

```python
class PipelineNodeRun(Base):
    __tablename__ = "pipeline_node_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(50), nullable=False)   # pipeline 啟動時的 timestamp 字串
    node_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)   # pending | running | done | error
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
```

- `run_id` 由 `dashboard.py` 在啟動 pipeline 前產生（例如 `time.time()` 轉字串或 uuid4），透過閉包傳給 `graph.py` 的 wrapper。
- 每個節點在**每次 run 開始前**，先為所有 7 個節點各寫一筆 `status="pending"`（讓前端一開始就能畫出完整的灰色流程圖，不用等第一個節點跑完才出現）。
- 需要一支 alembic migration。

### 2. `graph.py`：節點 wrapper

`build_graph()` 改為接受 `run_id`，對每個 `graph.add_node(name, fn)` 包一層：

```python
def _with_tracking(name: str, fn, run_id: str):
    def wrapped(state: AgentState) -> AgentState:
        _mark_status(run_id, name, "running", started_at=now())
        try:
            result = fn(state)
        except Exception as e:
            _mark_status(run_id, name, "error", finished_at=now(), error=str(e)[:500])
            raise
        _mark_status(run_id, name, "done", finished_at=now())
        return result
    return wrapped
```

- `_mark_status` 是新的小函式（可放在 `graph.py` 或新的 `pipeline_status.py`），每次呼叫各自開一個短生命週期的 `SessionLocal()` 寫入並 commit，避免長時間持有連線。
- 例外時**重新拋出**，不吞掉——保持現有錯誤處理行為不變（例如 `dashboard.py` 的 `_run_pipeline` 仍會抓到例外並記到 `_run_state`）。
- `build_graph(run_id: str | None = None)`：若 `run_id` 為 `None`（例如 `crawler.py` 單獨用 `if __name__ == "__main__"` 執行單一節點測試時的既有 `pipeline` 全域物件情境），略過 tracking，行為不變。

### 3. `dashboard.py`

- `start_run()`：產生 `run_id`，寫入 7 筆 `pending` 記錄，呼叫 `build_graph(run_id).invoke(...)`（背景 thread 不變）。
- `/status` 端點新增回傳欄位 `nodes: [{name, status, elapsed}, ...]`，依 `graph.py` 定義的固定順序（7 個節點）查最新 `run_id` 的記錄。若某節點還沒有記錄（極端 race condition），視為 `pending`。
- `run_id` 需要暫存在 `_run_state`（現有的全域 dict）供 `/status` 查詢用。

### 4. 前端

- 下載 `mermaid.min.js`，vendor 到 `src/library_agent/static/mermaid.min.js`，FastAPI 用 `app.mount("/static", StaticFiles(...))` 掛載。
- HTML 內嵌 Mermaid `flowchart LR` 語法，對應 7 節點 + edges（含 validator 後的平行分支）。
- JS 輪詢 `/status`（沿用現有 3 秒間隔），依回傳的 `nodes` 陣列用 `classDef`/`class` 語法動態改變節點顏色：
  - `pending` → 灰
  - `running` → 黃
  - `done` → 綠
  - `error` → 紅
- Mermaid 需要 `mermaid.initialize()` 一次，之後用 `mermaid.render()` 或直接操作已渲染 SVG 的 class 來更新顏色（避免每次輪詢整張圖重新渲染閃爍）。

## 資料流

```
使用者按「執行 pipeline」
  → dashboard 產生 run_id，寫入 7 筆 pending
  → 背景 thread 呼叫 build_graph(run_id).invoke(...)
  → 每個節點 wrapper 執行前後寫 DB (pipeline_node_runs)
  → 前端每 3 秒打 /status
  → /status 讀 DB 最新 run_id 的 7 節點狀態 + 現有 counts
  → 前端更新 Mermaid 圖節點顏色 + 現有 KPI/表格
```

## 測試

- 用 `?limit=2` 小跑一次，觀察 dashboard 流程圖節點顏色依序變化（灰→黃→綠）。
- 確認 `validator` 完成後 `librarian` 與 `human_review` 同時變黃（平行分支）。
- 確認 `pipeline_node_runs` 資料以 `run_id` 正確區隔，重跑不會與前次記錄混淆。
- 手動觸發一個節點錯誤（例如暫時斷網），確認該節點顯示紅色且 `/status` 回傳的 `error` 訊息正確。

## 不做的事

- 不做節點內部單筆進度（例如「validator 跑到第 350/1000 筆」）——現有 `/status` 的 `counts` 欄位（各表筆數）已間接反映這件事，不重複實作。
- 不用 WebSocket。
- 不改變現有錯誤處理 / retry 邏輯。
