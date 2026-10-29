"""節點處理進度回報：讓 dashboard 看得到「已處理 X / 共 Y 本」與最新一行訊息。

用法：graph._with_tracking 在節點執行期間 bind(run_id, node)，節點迴圈裡呼叫
report(processed, total, message)。沒有綁定時（單獨執行 python agents/xxx.py）report
是 no-op，所以節點程式不必判斷自己是不是被 pipeline 呼叫。

進度只是給人看的顯示資訊，因此是「盡力而為」：寫入失敗一律吞掉，絕不讓 pipeline 中斷。
"""
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass

from sqlalchemy import func, select, update

from library_agent.db.models import PipelineNodeRun
from library_agent.db.session import SessionLocal

_MIN_INTERVAL = 0.5   # 兩次寫入之間至少隔幾秒（節點每筆都回報，不節流會對 DB 狂寫）
_MAX_MESSAGE = 200    # 訊息最長字元數（書名、錯誤字串可能很長）
_now = time.monotonic  # 測試用 monkeypatch 替換；report 每次呼叫恰好呼叫一次


@dataclass
class _Binding:
    run_id: str
    node: str
    last_write: float | None = None


_current: ContextVar[_Binding | None] = ContextVar("progress_binding", default=None)


def bind(run_id: str, node: str) -> Token:
    """把目前執行環境綁到 (run_id, node)；回傳的 token 交給 unbind 還原。"""
    return _current.set(_Binding(run_id, node))


def unbind(token: Token) -> None:
    _current.reset(token)


def report(processed: int, total: int, message: str | None = None, unit: str = "本") -> None:
    """回報「處理到第 processed 筆、本次共 total 筆」，並附目前這筆的簡短名稱。"""
    binding = _current.get()
    if binding is None:
        return

    now = _now()
    # 節流：第一次、最後一筆（否則畫面會停在 9/10）、或離上次寫入夠久才寫
    due = (
        binding.last_write is None
        or processed >= total
        or now - binding.last_write >= _MIN_INTERVAL
    )
    if not due:
        return
    binding.last_write = now  # 先記時間：就算這次寫入失敗，也不要下一筆又立刻重試

    if message is not None:
        message = message[:_MAX_MESSAGE]
    try:
        # 只更新該節點在這次 run 的「最新一列」：discoverer 在重推迴路裡會被執行多次，
        # _with_tracking 每次都會新增一列，舊的那幾列不能被覆蓋。
        latest = (
            select(func.max(PipelineNodeRun.id))
            .where(
                PipelineNodeRun.run_id == binding.run_id,
                PipelineNodeRun.node_name == binding.node,
            )
            .scalar_subquery()
        )
        with SessionLocal() as session:
            session.execute(
                update(PipelineNodeRun)
                .where(PipelineNodeRun.id == latest)
                .values(processed=processed, total=total, unit=unit, message=message)
            )
            session.commit()
    except Exception:  # noqa: BLE001 — 進度只是顯示用，失敗不能影響節點
        pass
