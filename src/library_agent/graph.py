from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from library_agent.agents.crawler import crawler_node
from library_agent.agents.discoverer import discoverer_node
from library_agent.agents.librarian import librarian_node
from library_agent.agents.parser import parser_node
from library_agent.agents.recommender import recommender_node
from library_agent.agents.validator import validator_node
from library_agent.db.models import Citation
from library_agent.db.models import VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal
from library_agent.state import AgentState


def _human_review_node(state: AgentState) -> AgentState:
    """從 DB 讀出被 validator 標記為 requires_human_review 的書目並列出。
    資料來源是 DB（single source of truth），因此獨立重跑 / resume 也能正確列出目前的待審 backlog。"""
    course_ids = state.get("course_ids")
    with SessionLocal() as session:
        q = (
            select(Citation.course_id, Citation.title, Citation.confidence)
            .join(VerifiedBookDB, VerifiedBookDB.citation_id == Citation.id)
            .where(VerifiedBookDB.review_status == "pending")
        )
        if course_ids:
            q = q.where(Citation.course_id.in_(course_ids))
        rows = session.execute(q).all()

    if not rows:
        return {}
    print(f"\n需要人工審核的書目（{len(rows)} 筆）：")
    for course_id, title, confidence in rows:
        print(f"  [{course_id}] {title}  confidence={confidence:.2f}")
    return {}


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("crawler", crawler_node)
    graph.add_node("parser", parser_node)
    graph.add_node("discoverer", discoverer_node)
    graph.add_node("validator", validator_node)
    graph.add_node("librarian", librarian_node)
    graph.add_node("recommender", recommender_node)
    graph.add_node("human_review", _human_review_node)

    graph.add_edge(START, "crawler")
    graph.add_edge("crawler", "parser")
    graph.add_edge("parser", "discoverer")
    graph.add_edge("discoverer", "validator")

    # validator 之後同時走兩條分支（平行執行）
    graph.add_edge("validator", "librarian")
    graph.add_edge("validator", "human_review")

    graph.add_edge("librarian", "recommender")
    graph.add_edge("recommender", END)
    graph.add_edge("human_review", END)

    return graph.compile()


pipeline = build_graph()
