from library_agent.agents.discoverer import _needs_discovery


def test_needs_discovery():
    assert _needs_discovery("X", {"X"}) is False   # 已有書目 → 不需 discovery
    assert _needs_discovery("Y", {"X"}) is True     # 沒書目   → 需 discovery
    assert _needs_discovery("Z", set()) is True
