"""dashboard 處理進度區：/status 的進度欄位與進度卡片 HTML。

/status 的兩個測試用真實 DB 播種專用 run_id（沿用 tests/test_graph_routing.py 的作法），
結束時清掉並還原 _run_state。
"""
import pytest

from library_agent import dashboard
from library_agent.db.models import PipelineNodeRun
from library_agent.db.session import SessionLocal

RUN_ID = "TESTPROGRUN"


# ---------- _pct ----------

def test_pct_handles_zero_and_missing_total():
    # total 為 0 或沒回報（crawler、human_review、沒有待辦的節點）：進度條不得除以零
    assert dashboard._pct(None, None) == 0
    assert dashboard._pct(3, 0) == 0
    assert dashboard._pct(5, 10) == 50
    assert dashboard._pct(12, 10) == 100  # 不得超過 100


# ---------- _progress_card ----------

def test_progress_card_has_one_row_per_node_in_order():
    html = dashboard._progress_card(["crawler", "parser"])

    assert html.index('data-node="crawler"') < html.index('data-node="parser"')
    assert html.count('class="prow"') == 2


def test_progress_card_without_data_shows_dash():
    assert "—" in dashboard._progress_card(["crawler"])


# ---------- /status ----------

@pytest.fixture
def seeded_run(monkeypatch):
    monkeypatch.setitem(dashboard._run_state, "run_id", RUN_ID)
    yield
    with SessionLocal() as s:
        s.query(PipelineNodeRun).filter(PipelineNodeRun.run_id == RUN_ID).delete(
            synchronize_session=False)
        s.commit()


def _add(rows):
    with SessionLocal() as s:
        for node, status, processed, total, message in rows:
            s.add(PipelineNodeRun(run_id=RUN_ID, node_name=node, status=status,
                                  processed=processed, total=total,
                                  unit="本" if total is not None else None, message=message))
        s.commit()


def _node(result, name):
    return next(n for n in result["nodes"] if n["name"] == name)


def test_status_returns_latest_progress_per_node(seeded_run):
    # 同一節點同一 run 有多列（pending / running…）；只取最新那列的進度
    _add([
        ("validator", "pending", None, None, None),
        ("validator", "running", 1, 10, "書A"),
        ("validator", "running", 7, 10, "書B"),
    ])

    validator = _node(dashboard.status(), "validator")

    assert validator["status"] == "running"
    assert (validator["processed"], validator["total"], validator["message"]) == (7, 10, "書B")
    assert validator["unit"] == "本"
    # 沒有任何列的節點：狀態 pending、沒有進度
    parser = _node(dashboard.status(), "parser")
    assert parser["status"] == "pending"
    assert parser["processed"] is None and parser["total"] is None


def test_status_keeps_progress_after_node_finishes(seeded_run):
    # _with_tracking 結束時會「新增」一列 done（不是更新 running 那列），
    # 這列沒有進度；若只看最新一列，節點一跑完「120 / 120 本」就會變回「—」。
    _add([
        ("validator", "running", 120, 120, "最後一本"),
        ("validator", "done", None, None, None),
    ])

    validator = _node(dashboard.status(), "validator")

    assert validator["status"] == "done"                      # 狀態取最新一列
    assert (validator["processed"], validator["total"]) == (120, 120)  # 進度取最新一列「有進度的」
    assert validator["message"] == "最後一本"
