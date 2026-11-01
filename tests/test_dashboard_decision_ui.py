"""採購表的「決定冊數」欄、「需求 N」標記與 KPI（渲染，不碰資料庫）。

匯出 CSV 讀每格的 textContent：輸入框的值與「需求」「建議已從 A 變成 B」的提示都不能是文字節點，
由 CSS 畫（content: attr(...)），匯出程式再明確讀 data-* 與輸入框的值。
"""
import re

from library_agent import dashboard

CR = dashboard.CourseRef


def row(**kw):
    base = dict(priority="high", courses=[CR("課甲", "required", False)], title="T", status="missing",
                copies=5, rationale="r", rec_id=9, book_key="title:t", need=5)
    base.update(kw)
    return dashboard.RecRow(**base)


def table(r):
    return dashboard._rec_table([r])


def cell(html, cls):
    return re.search(rf'<td class="{cls}[^"]*"[^>]*>.*?</td>', html, re.S).group(0)


def test_the_header_has_a_decision_column_after_the_copies_column():
    html = table(row())

    assert html.index("<th>冊數</th>") < html.index("<th>決定冊數</th>") < html.index("<th>理由</th>")


def test_rec_row_shows_the_suggestion_the_need_marker_and_a_decision_box():
    html = table(row(copies=5, need=26))

    num = cell(html, "num")
    assert 'data-need="26"' in num and "cut" in num                  # 被上限截斷：需求 26，建議 5
    assert re.sub(r"<[^>]+>", "", num) == "5"                        # 文字內容只有系統建議
    assert re.search(r'<input class="dec-in" type="number" min="0" max="9999" step="1"', html)
    assert 'data-key="title:t"' in html and 'placeholder="採用建議"' in html


def test_cut_marker_only_when_the_need_exceeds_the_suggestion():
    assert "cut" not in cell(table(row(copies=5, need=5)), "num")
    assert "cut" not in cell(table(row(copies=0, need=0, priority="skip")), "num")
    assert "cut" in cell(table(row(copies=2, need=3)), "num")


def test_decision_box_is_prefilled_with_the_stored_decision():
    html = table(row(decided=12))

    assert 'value="12"' in html and 'data-saved="12"' in html and 'class="dec-in has"' in html


def test_a_zero_decision_is_shown_as_zero_not_blank():
    html = table(row(decided=0))

    assert 'value="0"' in html and 'class="dec-in has"' in html


def test_no_decision_means_a_blank_box_without_the_highlight():
    html = table(row())

    assert 'value=""' in html and 'class="dec-in"' in html


def test_legacy_row_has_no_decision_box():
    html = table(row(book_key=None))

    assert "dec-in" not in html and '<td class="dec"></td>' in html


def test_stale_decision_is_flagged_with_both_numbers():
    html = table(row(decided=12, stale_from=3, copies=5))

    dec = cell(html, "dec")
    assert 'data-stale="3"' in dec and 'data-now="5"' in dec


def test_no_stale_flag_when_the_suggestion_has_not_changed():
    assert "data-stale" not in table(row(decided=12))


def test_decision_cell_has_no_text_nodes():
    dec = cell(table(row(decided=12, stale_from=3)), "dec")

    assert re.sub(r"<[^>]+>", "", dec) == ""


def test_row_carries_the_final_copies_for_the_kpi_and_export():
    assert 'data-final="5"' in table(row())                          # 沒決定：最終＝系統建議
    assert 'data-final="12"' in table(row(decided=12))
    assert 'data-final="0"' in table(row(decided=0))


def test_book_key_is_escaped_in_the_attribute():
    html = table(row(book_key='"><script>alert(1)</script>'))

    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_kpi_has_system_and_adopted_totals():
    html = dashboard._kpi_tiles({"courses": 1, "citations": 1, "verified": 1, "pending": 0,
                                 "copies": 10, "copies_final": 7})

    assert "系統建議冊數" in html and "採用冊數" in html and "建議採購總冊數" not in html
    assert re.search(r'id="kpi-final"[^>]*>7<', html) or 'id="kpi-final">7<' in html


def test_kpi_adopted_falls_back_to_the_systems_number_when_missing():
    html = dashboard._kpi_tiles({"courses": 1, "citations": 1, "verified": 1, "pending": 0, "copies": 10})

    assert 'id="kpi-final">10<' in html
