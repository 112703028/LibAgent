# Validator → Discoverer 回饋迴路設計

## 目標與動機

現有 pipeline 是線性的：`Discoverer 推薦 → Validator 驗證 → 完`。AI 推薦的書若驗證失敗（查無此書），只標記 `not_found` 就停住，不會嘗試修正。

本設計加入 **Validator → Discoverer 的條件回饋迴路**：當 AI 推薦的書驗證失敗時，把失敗訊號回饋給 Discoverer，讓它重新推薦替代書目，形成「推薦 → 驗證 →（失敗）→ 重新推薦」的閉環，直到成功或達迭代上限。

### 論文層面的意義

這是本專案第一個真正的「多代理協作」證據：一個 agent（Validator）的輸出**改變另一個 agent（Discoverer）的行為**，而非單向傳遞資料。對應 TAAI「代理式 AI / 多代理協作」主題。故事線：
> 驗證器是瓶頸（發現）→ 幻覺形態是張冠李戴（機制）→ 用 agent 回饋迴路讓系統自我修正（本設計）→ 可稽核性作為架構價值（應用意義）

## 觸發條件（精確定義）

一筆 citation 會觸發重推，當且僅當同時滿足：

1. `raw_mention` 開頭是 `[AI推薦]`（只重推 AI 推薦的書，不動 Parser 從課綱抽出的書——那些即使驗證失敗，也可能是課綱真有列、只是資料庫沒收錄，換掉不合理）
2. 對應 verified_book 的 `source in ("not_found", "ncl_not_found")`
   - 這兩個值已經是「NCL/LOC **和** Google Books 兩層來源都查不到」的結果（`_validate_chinese`/`_validate_english` 的 fallback 跑完才會落到這裡）
3. `source == "quota_exceeded"` **不觸發**（那是 Google 當天配額用完，不是書不存在；等配額恢復重驗即可，換書是錯的）
4. 該課的 `discovery_iteration` 未達上限（建議上限 = 2，即最多重推 2 次）

## 架構變更

### 1. 資料流（graph.py 拓樸）

```
discoverer → validator ─┬─(有可重推的失敗書 & 未達上限)→ discoverer（重推）→ validator …
                        └─(無可重推 / 已達上限)────────→ librarian + human_review（原路）
```

新增一個路由函式 `_route_after_validator(state) -> str`：
- 查 DB 是否存在滿足上述觸發條件的 citation
- 有 → 回 `"discoverer"`；無 → 回 `"librarian"`
- 用 `graph.add_conditional_edges("validator", _route_after_validator, {...})` 取代現有的 `validator → librarian` 直接邊
- `validator → human_review` 這條邊的處理：human_review 只在最終（不再迴圈）時才有意義。設計上，`_route_after_validator` 回 `"librarian"`（不再迴圈）的那一支，同時也是進 human_review 的時機；回 `"discoverer"`（迴圈）那一支不進 human_review。實作方式：路由函式的目標 map 為 `{"discoverer": "discoverer", "librarian": "librarian"}`，human_review 改由 librarian 分支後或與 librarian 平行觸發（保持原本「validator 後平行兩支」語義，但只在最終那一輪）。**實作時以此為準：human_review 每門課最多執行一次，不在迴圈中重複觸發。**

### 2. 迭代計數（state.py）

`AgentState` 加欄位：
```python
discovery_iteration: int  # 目前第幾輪 discovery（0=初次，>=1=重推）
```
每繞一圈 discoverer 時 +1。達上限（2）時路由函式強制回 `"librarian"`。

### 3. 失敗舊書的處理（保留，不刪除）

Citation 表加欄位 `superseded_by_iteration: int | None`：
- 標記這筆 AI 推薦書在第幾輪被換掉（可追溯實驗結果）
- 保留原列，但 validator 的查詢與 `done_ids` 計算要**排除** `superseded_by_iteration IS NOT NULL` 的列，避免重複驗證舊書
- 需要一個 **Alembic migration** 加此欄位

新推薦的書寫成新 citation，`raw_mention` 標上輪次（如 `[AI推薦-第2輪]`），方便實驗追溯。

### 4. Discoverer 重推模式（discoverer.py）

現有 `discoverer_node` 用 `_needs_discovery`（課完全沒書才推），`_discover_one` 讀課綱推薦 3-5 本。要加並存的「重推模式」：

- `discovery_iteration == 0`（或無）→ 走現有初次推薦邏輯
- `discovery_iteration >= 1` → 走重推邏輯：
  - 只針對「有 AI 推薦書驗證失敗」的課
  - 撈出該課失敗書名清單 `failed_titles`
  - 呼叫新函式 `_rediscover_one(course, failed_titles)`：prompt 在原本基礎上加一段
    > 「以下推薦書經查證資料庫後不存在，請勿再推薦這些書名，改推薦其他真實存在的替代書目：{failed_titles}」
  - 把失敗的舊 citation 標記 `superseded_by_iteration = 當前輪次`
  - 新推薦寫成新 citation

### 5. Validator 查詢排除 superseded（validator.py）

`validator_node` 的 citation 查詢（第 249-252 行）與 `done_ids` 計算，都要加上 `Citation.superseded_by_iteration.is_(None)` 過濾，避免重驗已被換掉的書。

## 實驗設計（feedback_loop_experiment.py）

證明迴路價值：**同一批課，無迴路 vs 有迴路的驗證成功率對比**。

1. 抽 20-30 門「需 AI 推薦」的課（Parser 沒抓到書的課）
2. **對照組（無迴路）**：discoverer → validator 跑一次，記錄 AI 推薦書驗證成功率 = 成功數 / 推薦總數
3. **實驗組（有迴路）**：同一批課，帶迴路 pipeline（最多迭代 2 次），記錄最終驗證成功率
4. 輸出：`無迴路 X%` vs `有迴路 Y%`、平均迭代次數、每輪修正幾本
5. 唯一變數是「有無迴路」，其餘（抽樣、模型、prompt 基礎）相同，確保公平

客觀數字（驗證成功率），**不需人工標註**。

## 動工清單（實作順序）

1. Alembic migration：Citation 加 `superseded_by_iteration`
2. `state.py`：AgentState 加 `discovery_iteration`
3. `discoverer.py`：加 `_rediscover_one` + node 分支邏輯 + 標記 superseded
4. `validator.py`：查詢與 done_ids 排除 superseded
5. `graph.py`：加 `_route_after_validator` 條件邊路由 + AgentState 初始化 `discovery_iteration`
6. `experiments/feedback_loop_experiment.py`：對照實驗
7. 測試：路由函式單元測試（達上限停 / 無失敗書停 / quota_exceeded 不觸發 / superseded 不重驗）

## 已知風險

- **進度追蹤**：迴路會讓 `_with_tracking` 的 discoverer/validator 節點每輪各標記一次，dashboard 流程圖顯示可能需要微調（例如顯示「第 N 輪」）。不影響 pipeline 正確性；若顯示影響大，實作時回報。
- **迭代成本**：每輪多一次 LLM 呼叫（discoverer）+ API 查詢（validator）。上限設 2 控制成本。
- **human_review 迴圈重複**：需確認 human_review 不會在每輪迴圈被重複觸發，只在最終進 librarian 那一支時執行。
