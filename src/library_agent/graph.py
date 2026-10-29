import os
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from library_agent import runlog
from library_agent.agents.crawler import crawler_node
from library_agent.agents.discoverer import _failed_ai_citations, discoverer_node
from library_agent.agents.librarian import librarian_node
from library_agent.agents.match_auditor import match_auditor_node
from library_agent.agents.parser import parser_node
from library_agent.agents.recommender import recommender_node
from library_agent.agents.validator import validator_node
from library_agent.db.models import Citation
from library_agent.db.models import Course
from library_agent.db.models import PipelineNodeRun
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.state import AgentState

# 回饋迴路最大重推輪數；可用環境變數 MAX_DISCOVERY_ITER 覆寫（實驗用）。
_MAX_DISCOVERY_ITERATIONS = int(os.environ.get("MAX_DISCOVERY_ITER", "2"))

# 固定順序，對應 build_graph() 的拓樸；dashboard.py 的 /status 依此順序回傳節點狀態。
NODE_NAMES = [
    "crawler", "parser", "discoverer", "validator", "match_auditor",
    "librarian", "human_review", "recommender",
]


def _mark_status(
    run_id: str,
    node_name: str,
    status: str,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """寫一筆節點狀態記錄。每次呼叫用短生命週期的 session，立即 commit。"""
    with SessionLocal() as session:
        session.add(PipelineNodeRun(
            run_id=run_id,
            node_name=node_name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
        ))
        session.commit()


def _init_run(run_id: str) -> None:
    """pipeline 開跑前，為所有節點各寫一筆 pending，讓前端一開始就能畫出完整流程圖。"""
    for name in NODE_NAMES:
        _mark_status(run_id, name, "pending")


def _with_tracking(name: str, fn, run_id: str):
    def wrapped(state: AgentState) -> AgentState:
        _mark_status(run_id, name, "running", started_at=datetime.now(timezone.utc))
        # 終端文字 log 每行的 [節點名稱] 前綴：節點裡的 print() 不帶節點名稱，靠這個綁定。
        token = runlog.bind_node(name)
        try:
            result = fn(state)
        except Exception as e:
            _mark_status(run_id, name, "error", finished_at=datetime.now(timezone.utc), error=str(e)[:500])
            raise
        finally:
            # 一定要解除：沒解除的綁定會殘留在這條執行緒，之後別的行會被標成錯的節點
            runlog.unbind_node(token)
        _mark_status(run_id, name, "done", finished_at=datetime.now(timezone.utc))
        return result
    return wrapped


def _human_review_node(state: AgentState) -> AgentState:
    """從 DB 讀出被標記為 requires_human_review 的書目並列出（來源：validator 的低信心、
    或 match_auditor 判定配對可疑）。資料來源是 DB（single source of truth），
    因此獨立重跑 / resume 也能正確列出目前的待審 backlog。"""
    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        q = (
            select(
                Citation.course_id, Citation.title, Citation.confidence,
                VerifiedBookDB.canonical_title, VerifiedBookDB.pairing_verdict,
                VerifiedBookDB.pairing_reason,
            )
            .join(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
            .where(VerifiedBookDB.review_status == "pending")
        )
        if course_ids:
            q = q.where(Citation.course_id.in_(course_ids))
        rows = session.execute(q).all()

    if not rows:
        return {}
    print(f"\n需要人工審核的書目（{len(rows)} 筆）：")
    for course_id, title, confidence, matched, verdict, reason in rows:
        print(f"  [{course_id}] {title}  confidence={confidence:.2f}")
        if verdict == "mismatch":
            print(f"      ⚠ 配對可疑 → 配到：{matched}")
            print(f"        理由：{reason}")
    return {}


def _route_after_validator(state: AgentState) -> str:
    """驗證後決定：有可重推的 AI 失敗書且未達上限 → discoverer（迴路）；否則 → match_auditor（下游）。
    只讀 discovery_iteration，不寫；真正的遞增在 validator_node。"""
    iteration = state.get("discovery_iteration", 0)
    if iteration >= _MAX_DISCOVERY_ITERATIONS:
        print(f"[迴路] 已達迭代上限 {_MAX_DISCOVERY_ITERATIONS} → 進 match_auditor", flush=True)
        return "match_auditor"

    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        q = select(Course.course_id)
        if course_ids:
            q = q.where(Course.course_id.in_(course_ids))
        all_course_ids = [cid for (cid,) in session.execute(q).all()]
        for cid in all_course_ids:
            if _failed_ai_citations(session, cid):
                print(f"[迴路] 課程 {cid} 有 AI 推薦書驗證失敗 → 回 discoverer 重推"
                      f"（下一輪 iteration={iteration + 1}）", flush=True)
                return "discoverer"
    print("[迴路] 無可重推書目 → 進 match_auditor", flush=True)
    return "match_auditor"


def build_graph(run_id: str | None = None) -> StateGraph:
    graph = StateGraph(AgentState)

    nodes = {
        "crawler": crawler_node,
        "parser": parser_node,
        "discoverer": discoverer_node,
        "validator": validator_node,
        "match_auditor": match_auditor_node,
        "librarian": librarian_node,
        "recommender": recommender_node,
        "human_review": _human_review_node,
    }
    for name, fn in nodes.items():
        graph.add_node(name, _with_tracking(name, fn, run_id) if run_id else fn)

    graph.add_edge(START, "crawler")
    graph.add_edge("crawler", "parser")
    graph.add_edge("parser", "discoverer")
    graph.add_edge("discoverer", "validator")

    # validator 後：條件邊決定「回 discoverer 重推」或「進 match_auditor（下游）」
    graph.add_conditional_edges(
        "validator",
        _route_after_validator,
        {"discoverer": "discoverer", "match_auditor": "match_auditor"},
    )

    # 配對稽核必須在 librarian / recommender 之前：recommender 只排除 review_status='rejected'，
    # 所以配錯的書若沒先被標記，會一路被推成採購建議。
    graph.add_edge("match_auditor", "librarian")
    graph.add_edge("match_auditor", "human_review")

    graph.add_edge("librarian", "recommender")
    graph.add_edge("recommender", END)
    graph.add_edge("human_review", END)

    return graph.compile()


pipeline = build_graph()
