"""dashboard 的類別篩選（指定 / 參考 / AI 推薦）與學系篩選同步。"""
from library_agent import dashboard


# ---------- 類別篩選 ----------

def test_book_type_key():
    assert dashboard._book_type_key(True, "指定") == "required"
    assert dashboard._book_type_key(False, "參考") == "optional"
    assert dashboard._book_type_key(True, "[AI推薦] 理由") == "ai"   # AI 推薦優先於指定
    assert dashboard._book_type_key(False, None) == "optional"


def test_rec_row_carries_category_token():
    # data-btypes 是空白分隔的 token 清單（現在只有一個；第 4 點合併課程後會有多個，前端不必再改）
    row = dashboard._rec_row("high", "課", "書", "<b>", "s", "1", "r", "required")

    assert 'data-btypes="required"' in row


def test_rec_table_rows_have_category_tokens():
    recs = [("high", "課", "書A", True, "missing", 1, "理由", "指定")]
    pending = [("課", "書B", 0.5, "nla", False, "[AI推薦] x")]

    html = dashboard._rec_table(recs, pending)

    assert 'data-btypes="required"' in html
    assert 'data-btypes="ai"' in html


def test_category_buttons_sit_beside_priority_buttons():
    html = dashboard._render(dashboard._load_data())

    for key in ("all", "required", "optional", "ai"):
        assert f'data-c="{key}"' in html, f"缺少類別按鈕 {key}"
    assert html.index('data-f="all"') < html.index('data-c="all"')   # 類別按鈕在優先級按鈕之後


# ---------- 學系篩選（後端） ----------
#
# 沿用 tests/test_graph_routing.py 的作法：用真實 DB 播種專用課程、結束時清掉。
# 三個課程的學系：TESTDEPT_A、測試學系_B（刻意用中文，涵蓋網址編碼）、None（沒有學系）。

import pytest
from fastapi.testclient import TestClient

from library_agent.db.models import (
    Citation, Course, HoldingCheck, Recommendation, VerifiedBook,
)
from library_agent.db.session import SessionLocal

_DEPT_A, _DEPT_B = "TESTDEPT_A", "測試學系_B"
_SEEDS = [  # (course_id, department, 書名)
    ("TESTFA", _DEPT_A, "TESTBOOK-A"),
    ("TESTFB", _DEPT_B, "TESTBOOK-B"),
    ("TESTFC", None, "TESTBOOK-C"),
]


_BLK_COURSE, _BLK_DEPT = "TESTFD", "TESTDEPT_D"   # 專門測「被採購條件擋掉的書」的課


def _cleanup():
    ids = [c for c, _, _ in _SEEDS] + [_BLK_COURSE]
    with SessionLocal() as s:
        cit_ids = [i for (i,) in s.query(Citation.id).filter(Citation.course_id.in_(ids))]
        vb_ids = [i for (i,) in s.query(VerifiedBook.id).filter(VerifiedBook.citation_id.in_(cit_ids))]
        hc_ids = [i for (i,) in s.query(HoldingCheck.id).filter(HoldingCheck.verified_book_id.in_(vb_ids))]
        s.query(Recommendation).filter(Recommendation.holding_id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(HoldingCheck).filter(HoldingCheck.id.in_(hc_ids)).delete(synchronize_session=False)
        s.query(VerifiedBook).filter(VerifiedBook.id.in_(vb_ids)).delete(synchronize_session=False)
        s.query(Citation).filter(Citation.id.in_(cit_ids)).delete(synchronize_session=False)
        s.query(Course).filter(Course.course_id.in_(ids)).delete(synchronize_session=False)
        s.commit()


@pytest.fixture
def seeded():
    _cleanup()
    with SessionLocal() as s:
        for course_id, dept, title in _SEEDS:
            s.add(Course(course_id=course_id, course_name=f"課-{course_id}", semester="9999",
                         source_file="test", raw_content="test", department=dept))
            s.flush()
            c = Citation(course_id=course_id, title=title, authors="[]", is_required=True,
                         raw_mention="指定", confidence=0.9)
            s.add(c)
            s.flush()
            # B 的書標成待審，用來驗證「待審核明細」也有被限縮
            vb = VerifiedBook(citation_id=c.id, canonical_title=title, canonical_authors="[]",
                              source="nla", verified=True,
                              review_status="pending" if dept == _DEPT_B else None)
            s.add(vb)
            s.flush()
            hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
            s.add(hc)
            s.flush()
            s.add(Recommendation(course_id=course_id, holding_id=hc.id, priority="high",
                                 suggested_copies=1, rationale="r"))
        s.commit()
    yield
    _cleanup()


def _titles(data):
    return {r[2] for r in data["recs"]}


def test_expand_departments():
    assert dashboard._expand_departments(None) is None
    assert dashboard._expand_departments([]) is None
    assert dashboard._expand_departments([_DEPT_A]) == [_DEPT_A]   # 未知名稱原樣當原始值
    raw, (_college, dep) = next(iter(dashboard.DEPARTMENT_MAP.items()))
    assert raw in dashboard._expand_departments([dep])


def test_load_data_scopes_every_section_to_departments(seeded):
    a = dashboard._load_data([_DEPT_A])

    assert a["kpis"]["courses"] == 1
    assert a["kpis"]["citations"] == 1
    assert a["kpis"]["verified"] == 1
    assert a["kpis"]["pending"] == 0
    assert a["kpis"]["copies"] == 1
    assert a["priority"]["high"] == 1
    assert a["status"]["missing"] == 1
    assert _titles(a) == {"TESTBOOK-A"}
    assert a["pending"] == []

    b = dashboard._load_data([_DEPT_B])
    assert _titles(b) == {"TESTBOOK-B"}
    assert b["kpis"]["pending"] == 1
    assert [p[1] for p in b["pending"]] == ["TESTBOOK-B"]


def test_load_data_without_filter_includes_everything_including_null_department(seeded):
    # 沒有學系的課在未篩選時照常出現；選任何學系時都不會出現
    assert {"TESTBOOK-A", "TESTBOOK-B", "TESTBOOK-C"} <= _titles(dashboard._load_data())
    assert "TESTBOOK-C" not in _titles(dashboard._load_data([_DEPT_A]))
    assert "TESTBOOK-C" not in _titles(dashboard._load_data([_DEPT_B]))


def test_load_data_unknown_department_is_empty_and_renders():
    data = dashboard._load_data(["NO_SUCH_DEPT"])

    assert data["kpis"]["courses"] == 0 and data["kpis"]["citations"] == 0
    assert data["kpis"]["verified"] == 0 and data["kpis"]["copies"] == 0
    assert data["recs"] == [] and data["pending"] == []
    assert _has(dashboard._render(data), "尚無採購建議")   # 空表提示，不得出錯


def _has(text: str, needle: str) -> bool:
    # 頁面 HTML 有 1 MB；直接 assert "x" not in text 失敗時，pytest 會對整份 HTML 做逐行差異比對，
    # 慢到像卡死。包成只回傳布林值的函式，失敗時只會印 True/False。
    return needle in text


def test_index_route_filters_by_repeated_departments_param(seeded):
    client = TestClient(dashboard.app)

    only_a = client.get("/", params=[("departments", _DEPT_A)])
    assert only_a.status_code == 200
    assert _has(only_a.text, "TESTBOOK-A")
    assert not _has(only_a.text, "TESTBOOK-B")

    only_b = client.get("/", params=[("departments", _DEPT_B)])   # 中文學系名走網址編碼
    assert _has(only_b.text, "TESTBOOK-B")
    assert not _has(only_b.text, "TESTBOOK-A")

    both = client.get("/", params=[("departments", _DEPT_A), ("departments", _DEPT_B)])
    assert _has(both.text, "TESTBOOK-A") and _has(both.text, "TESTBOOK-B")

    unfiltered = client.get("/")
    assert _has(unfiltered.text, "TESTBOOK-A") and _has(unfiltered.text, "TESTBOOK-B")


# ---------- 館藏狀態圖與優先級圖用同一個「可採購」條件 ----------

@pytest.fixture
def seeded_with_blocked_book():
    """一門課兩本書：一本正常；一本被 match_auditor 判配錯、人工尚未放行（採購條件會擋掉）。"""
    _cleanup()
    with SessionLocal() as s:
        s.add(Course(course_id=_BLK_COURSE, course_name="課-TESTFD", semester="9999",
                     source_file="test", raw_content="test", department=_BLK_DEPT))
        s.flush()
        for title, verdict in (("TESTBOOK-OK", None), ("TESTBOOK-MISMATCH", "mismatch")):
            c = Citation(course_id=_BLK_COURSE, title=title, authors="[]", is_required=True,
                         raw_mention="指定", confidence=0.9)
            s.add(c)
            s.flush()
            vb = VerifiedBook(citation_id=c.id, canonical_title=title, canonical_authors="[]",
                              source="nla", verified=True, pairing_verdict=verdict)
            s.add(vb)
            s.flush()
            hc = HoldingCheck(verified_book_id=vb.id, status="missing", holdings_count=0)
            s.add(hc)
            s.flush()
            s.add(Recommendation(course_id=_BLK_COURSE, holding_id=hc.id, priority="high",
                                 suggested_copies=1, rationale="r"))
        s.commit()
    yield
    _cleanup()


def test_status_chart_excludes_books_the_purchase_gate_blocks(seeded_with_blocked_book):
    d = dashboard._load_data([_BLK_DEPT])

    assert sum(d["priority"].values()) == 1           # 優先級圖本來就套用採購條件
    assert sum(d["status"].values()) == 1             # 館藏狀態圖也要：配錯未放行的那本不能被算進去
    assert {r[2] for r in d["recs"]} == {"TESTBOOK-OK"}
