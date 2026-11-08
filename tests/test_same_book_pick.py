"""從多筆候選中挑出「同一本」（書商後備用），沿用 match_auditor 的階梯式判斷：
字串規則判不同本的不問 LLM；其餘依規則相似度排序，最多問前 3 名，第一個判同一本的採用；LLM 失敗＝不同本。
"""
from types import SimpleNamespace

from library_agent.agents import match_auditor as M


def fake_llm(same_titles=()):
    calls = []

    def llm(orig_title, orig_authors, cand_title, cand_authors):
        calls.append(cand_title)
        return cand_title in same_titles, f"理由:{cand_title}"

    llm.calls = calls
    return llm


def test_rule_verdict_marks_clear_mismatches_and_defers_the_rest():
    assert M.rule_verdict("社會學", "量子力學導論") == "mismatch"
    assert M.rule_verdict("社會學", "社會學（第三版）") is None                     # 包含關係＝滿分，要問 LLM
    assert M.rule_verdict("Statistics for Business", "商務統計") is None           # 跨文字：規則沒有信號


def test_rule_mismatch_candidates_are_never_sent_to_the_llm():
    llm = fake_llm()

    idx, _reason = M.pick_same_book("社會學", [], [("量子力學導論", [])], llm)

    assert idx is None and llm.calls == []


def test_candidates_are_asked_in_rule_score_order_and_the_first_same_wins():
    orig = "台灣電影饗宴百年導覽"
    cands = [("台灣電影史百年導覽", []), ("台灣電影饗宴", []), ("台灣電影饗宴百年導覽", [])]
    order = sorted(range(3), key=lambda i: -M.rule_score(orig, cands[i][0]))
    llm = fake_llm(same_titles={cands[order[1]][0]})                              # 只有第二高分的是同一本

    idx, reason = M.pick_same_book(orig, [], cands, llm)

    assert idx == order[1]                                                         # 索引對應原本的候選清單
    assert llm.calls == [cands[order[0]][0], cands[order[1]][0]]                  # 由高到低問，找到就停
    assert reason == f"理由:{cands[order[1]][0]}"


def test_at_most_three_candidates_are_asked():
    cands = [(f"社會學 第{i}版", []) for i in range(5)]
    llm = fake_llm()

    idx, _reason = M.pick_same_book("社會學", [], cands, llm)

    assert idx is None and len(llm.calls) == 3


def test_cross_script_candidates_go_to_the_llm_after_rule_passing_ones():
    llm = fake_llm()

    M.pick_same_book("Statistics for Business", [],
                     [("商務統計", []), ("Statistics for Business and Economics", [])], llm)

    assert llm.calls == ["Statistics for Business and Economics", "商務統計"]


def test_no_candidates_returns_none():
    assert M.pick_same_book("社會學", [], [], fake_llm()) == (None, "沒有候選")


def test_bookstore_llm_counts_a_failure_as_different(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("API down")

    broken = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    monkeypatch.setattr(M, "_new_client", lambda: broken)

    same, reason = M.bookstore_llm()("社會學", [], "社會學", [])

    assert same is False and "失敗" in reason                                     # 寧可不算驗證成功，也不要配錯
