"""book_identity：決定「兩筆已驗證的書是不是同一本」的識別鍵。

規則（見設計文件第 4 點）：有效 ISBN 才算 ISBN；沒有有效 ISBN 才靠書名分桶，桶內相同者同組、
其餘交給判斷函式（重用 match_auditor 的規則＋LLM）；判斷失敗時不合併。
"""
import pytest

from library_agent import book_identity as bi
from library_agent.agents import match_auditor as ma


def rec(i, title, authors=(), isbn=None):
    return {"id": i, "title": title, "authors": list(authors), "isbn": isbn}


def _never_called(a, b):
    raise AssertionError("這組不該呼叫判斷函式")


def _same_authors(a, b):
    return set(a["authors"]) == set(b["authors"])


# ---------- normalize_isbn ----------

def test_normalize_isbn():
    assert bi.normalize_isbn("978-0-306-40615-7") == "9780306406157"
    assert bi.normalize_isbn("0306406152") == "9780306406157"        # ISBN-10 轉成 13 碼
    assert bi.normalize_isbn("9780306406158") is None                  # 檢查碼錯
    assert bi.normalize_isbn("991010871719705721") is None             # Alma 記錄編號，不是 ISBN
    assert bi.normalize_isbn("") is None
    assert bi.normalize_isbn(None) is None


# ---------- assign_book_keys：有 ISBN ----------

def test_isbn10_and_isbn13_of_the_same_book_share_a_key():
    keys = bi.assign_book_keys(
        [rec(1, "A", isbn="0306406152"), rec(2, "A", isbn="978-0-306-40615-7")], _never_called)

    assert keys[1] == keys[2] == "isbn:9780306406157"


def test_different_valid_isbns_are_different_books_even_with_the_same_title():
    # 不同版次：寧可多買一套，也不要把不同的書當成同一本
    keys = bi.assign_book_keys(
        [rec(1, "Economics", ["Parkin"], "9780321131034"), rec(2, "Economics", ["Parkin"], "9780133872279")],
        _never_called)

    assert keys[1] != keys[2]


# ---------- assign_book_keys：沒有有效 ISBN ----------

def test_invalid_isbn_falls_back_to_title_and_judge():
    # 兩本作者不同的《經濟學》共用同一個 Alma 記錄編號：照「ISBN 相同就是同一本」會被錯併
    alma = "991010871719705721"
    keys = bi.assign_book_keys(
        [rec(1, "經濟學", ["許嘉棟"], alma), rec(2, "經濟學", ["毛慶生"], alma)], _same_authors)

    assert keys[1] != keys[2]


def test_identical_records_skip_the_judge():
    keys = bi.assign_book_keys(
        [rec(1, "微積分", ["王"]), rec(2, "微積分", ["王"])], _never_called)

    assert keys[1] == keys[2]


def test_title_normalisation_ignores_case_punctuation_and_script_variants():
    keys = bi.assign_book_keys(
        [rec(1, "Principles of Economics"), rec(2, "principles  of economics!")], _never_called)

    assert keys[1] == keys[2]


def test_title_clusters_get_numbered_keys():
    keys = bi.assign_book_keys(
        [rec(1, "Economics", ["A"]), rec(2, "Economics", ["B"]), rec(3, "Economics", ["A"])], _same_authors)

    assert keys[1] == keys[3] == "title:economics"
    assert keys[2] == "title:economics#2"


def test_isbn_book_never_merges_with_no_isbn_book_of_same_title():
    keys = bi.assign_book_keys(
        [rec(1, "Economics", ["A"], "9780306406157"), rec(2, "Economics", ["A"])], lambda a, b: True)

    assert keys[1] == "isbn:9780306406157"
    assert keys[2] == "title:economics"


def test_empty_title_is_never_merged():
    keys = bi.assign_book_keys([rec(1, ""), rec(2, "")], lambda a, b: True)

    assert keys[1] == "rec:1" and keys[2] == "rec:2"


def test_keys_are_deterministic_regardless_of_input_order():
    records = [rec(3, "Economics", ["A"]), rec(1, "Economics", ["B"]), rec(2, "Economics", ["A"])]

    forward = bi.assign_book_keys(records, _same_authors)
    backward = bi.assign_book_keys(list(reversed(records)), _same_authors)

    assert forward == backward


def test_very_long_titles_are_truncated_to_fit_the_column():
    key = bi.assign_book_keys([rec(1, "x" * 900)], _never_called)[1]

    assert len(key) <= 500


# ---------- 判斷器：失敗時不合併 ----------

class _DownClient:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("LLM down")


def test_llm_judge_failure_means_not_merged():
    judge = bi.llm_judge(_DownClient, "m")

    assert judge(rec(1, "經濟學", ["許嘉棟"]), rec(2, "經濟學", ["毛慶生"])) is False


def test_llm_same_book_default_still_fails_as_same():
    # match_auditor 稽核時失敗要保守判「同一本」（避免整批誤送審）；去重才需要相反的方向
    same, _ = ma._llm_same_book(_DownClient, "m", "a", [], "b", [])
    not_same, _ = ma._llm_same_book(_DownClient, "m", "a", [], "b", [], fail_as_same=False)

    assert same is True
    assert not_same is False


def test_a_judge_that_raises_means_not_merged():
    # 判斷函式出了任何意外都不能讓整個 recommender 崩潰，也不能誤併
    def boom(a, b):
        raise ValueError("unexpected")

    keys = bi.assign_book_keys([rec(1, "Economics", ["A"]), rec(2, "Economics", ["B"])], boom)

    assert keys[1] != keys[2]
