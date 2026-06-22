from langgraph.graph import END, START, StateGraph

from library_agent.agents.crawler import crawler_node
from library_agent.agents.discoverer import discoverer_node
from library_agent.agents.librarian import librarian_node
from library_agent.agents.parser import parser_node
from library_agent.agents.recommender import recommender_node
from library_agent.agents.validator import validator_node
from library_agent.state import AgentState


def _human_review_node(state: AgentState) -> AgentState:
    queue = state.get("human_review_queue", [])
    if not queue:
        return {}
    print(f"\n需要人工審核的書目（{len(queue)} 筆）：")
    for book in queue:
        print(f"  [{book.course_id}] {book.title}  confidence={book.confidence:.2f}")
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
