from library_agent.state import AgentState


def test_agent_state_has_discovery_iteration():
    # AgentState 是 TypedDict(total=False)，欄位存在性靠型別註解；此測試確認欄位已宣告。
    assert "discovery_iteration" in AgentState.__annotations__
    assert AgentState.__annotations__["discovery_iteration"] is int
