"""各分頁統一的版面：分頁列（含採購單）、共用工具列、欄位骨架、批次列、列上的資料屬性。

統一的骨架：每張表第一格是勾選，其次「優先級｜課程｜▸ 書名｜…」，最右一欄是「處理」；
每個面板頂端有同一種批次列；工具列（搜尋、篩選、匯出）只有一條，對目前開著的分頁生效。
採購單不是另一張表，而是採購建議那張表的檢視（只顯示 data-incart 的列），所以只有一個分頁按鈕、沒有自己的面板。
"""
import re

from library_agent import dashboard
from library_agent.review_groups import KINDS
from tests.test_dashboard_review import grp

CR = dashboard.CourseRef


def rec(**kw):
    base = dict(priority="high", courses=[CR("課甲", "required", False)], title="Intro", status="missing",
                copies=5, rationale="r", rec_id=9, book_key="title:intro", need=5)
    base.update(kw)
    return dashboard.RecRow(**base)


def page(recs=None, review=None, decided=None):
    recs = [rec()] if recs is None else recs
    data = {"kpis": {"courses": 1, "citations": 1, "verified": 1, "pending": 0, "copies": 5,
                     "copies_final": sum(r.final for r in recs), "cart_books": sum(r.in_cart for r in recs)},
            "priority": {"high": 1}, "status": {"missing": 1}, "recs": recs, "pending": [],
            "review": review or {k: [] for k in KINDS}, "decided": decided or [], "pending_apply": 0}
    return dashboard._render(data)


def reviews():
    review = {k: [] for k in KINDS}
    review["mismatch"] = [grp("mismatch")]
    review["notfound"] = [grp("notfound", "NF", vb_ids=(3,), source="ncl_not_found", judge=None, reason=None,
                              priority="high", copies=2)]
    review["other"] = [grp("other", "OT", vb_ids=(4,), judge=None, reason=None)]
    return review


def decided_group(**kw):
    import dataclasses
    return dataclasses.replace(grp("mismatch", "DONE", vb_ids=(7,)), choice="original", buy_title="買的書", **kw)


def tabs(html):
    return re.findall(r'<button class="rtab[^"]*" data-k="(\w+)"([^>]*)>([^<]*)</button>', html)


def panels(html):
    """面板開頭標籤的屬性與內容：{data-k: (標籤字串, 內容)}"""
    out = {}
    for chunk in html.split('<div class="rpanel"')[1:]:
        head = chunk[:chunk.index(">")]
        out[re.search(r'data-k="(\w+)"', head).group(1)] = (head, chunk)
    return out


def headers(table_html):
    return [re.sub(r"<[^>]+>", "", th) for th in re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.S)]


def table_of(panel_chunk):
    return re.search(r"<table.*?</table>", panel_chunk, re.S).group(0)


# ---------- 分頁列 ----------

def test_tab_bar_order_and_cart_tab_label():
    html = page([rec(decided=12)], reviews(), [decided_group()])

    assert [t[0] for t in tabs(html)] == ["recs", "cart", "mismatch", "notfound", "other", "decided"]
    cart = next(t for t in tabs(html) if t[0] == "cart")
    assert cart[2] == "採購單 1 本·12 冊" and 'data-panel="recs"' in cart[1]


def test_cart_tab_shares_the_purchase_panel_it_has_no_panel_of_its_own():
    html = page([rec(decided=12)], reviews())

    assert 'data-k="cart"' in html and html.count('data-k="cart"') == 1        # 只有分頁按鈕
    assert "cart" not in panels(html)


def test_cart_tab_label_when_nothing_is_in_the_cart():
    html = page([rec(), rec(decided=0, book_key="title:b", rec_id=10, title="B")])

    assert next(t for t in tabs(html) if t[0] == "cart")[2] == "採購單 0 本·0 冊"      # 0＝明確排除，不算


def test_review_log_tab_is_named_review_log():
    html = page([rec()], reviews(), [decided_group()])

    label = next(t for t in tabs(html) if t[0] == "decided")[2]
    assert label == "審核紀錄 1 組" and "已決定" not in label


def test_tab_bar_is_present_with_the_purchase_list_and_cart_even_without_review_items():
    assert [t[0] for t in tabs(page())] == ["recs", "cart"]


# ---------- 共用工具列 ----------

def test_one_toolbar_with_search_filters_and_export_between_the_tab_bar_and_the_panels():
    html = page([rec()], reviews())

    assert html.count('id="toolbar"') == 1 and html.count('id="recsearch"') == 1 and html.count('id="expbtn"') == 1
    assert 'class="tb-prio"' in html and 'class="tb-cat"' in html
    assert html.index('class="filters maintabs"') < html.index('id="toolbar"') < html.index('class="rpanel"')


def test_each_panel_declares_which_filters_apply():
    got = {k: re.search(r'data-filters="([^"]*)"', head).group(1)
           for k, (head, _c) in panels(page([rec()], reviews(), [decided_group()])).items()}

    assert got == {"recs": "prio cat", "mismatch": "", "notfound": "prio", "other": "prio", "decided": ""}


def test_each_panel_declares_its_export_kind():
    got = {k: re.search(r'data-export="([^"]*)"', head).group(1)
           for k, (head, _c) in panels(page([rec()], reviews(), [decided_group()])).items()}

    assert got == {"recs": "recs", "mismatch": "review", "notfound": "review", "other": "review", "decided": "log"}


def test_the_export_priority_checkboxes_sit_in_their_own_block_so_the_front_end_can_hide_them():
    assert 'class="exp-prio"' in page()


def test_the_pending_csv_link_is_gone_but_the_endpoint_remains():
    html = page([rec()], reviews())

    assert "匯出待審清單" not in html
    assert any(getattr(r, "path", "") == "/export/pending" for r in dashboard.app.routes)


# ---------- 欄位骨架 ----------

def test_purchase_table_keeps_its_column_order_with_the_decision_at_the_far_right():
    table = table_of(panels(page())["recs"][1])

    assert headers(table) == ["", "優先級", "課程", "書名", "類別", "館藏狀態", "冊數", "理由", "處理"]


def test_review_tables_start_with_checkbox_priority_course_title_and_end_with_actions():
    pn = panels(page([rec()], reviews()))
    expected = ["", "優先級", "用到的課（人數）", "原書（課綱列的）", "配到的書", "原因", "人數合計", "處理"]

    for kind in ("mismatch", "notfound", "other"):
        assert headers(table_of(pn[kind][1])) == expected


def test_review_log_has_checkbox_expand_and_a_course_column():
    chunk = panels(page([rec()], reviews(), [decided_group()]))["decided"][1]

    assert headers(table_of(chunk)) == ["", "用到的課", "原書（課綱列的）", "決定", "採購書名", "狀態", "處理"]
    assert 'class="rchk"' in chunk and 'class="tbtn"' in chunk and "data-undo" in chunk
    assert 'class="rchkall"' in chunk


def test_the_review_priority_cell_shows_the_priority_pill_or_why_it_is_not_in_the_list():
    pn = panels(page([rec()], reviews()))

    assert dashboard._PRIORITY_LABEL["high"] in pn["notfound"][1]                 # 查無此書：有採購建議的優先級
    assert "已擋下，不在採購清單" in pn["mismatch"][1]                               # 配錯：被擋下


# ---------- 列上的資料屬性 ----------

def test_rows_carry_data_search_lowercase_and_escaped():
    html = page([rec(title='Intro <B> "Q"')], reviews())

    search = re.search(r'<tr [^>]*data-search="([^"]*)"', panels(html)["recs"][1]).group(1)
    assert "intro &lt;b&gt; &quot;q&quot;" in search and "課甲" in search           # 小寫、逸出、含課程
    assert "<b>" not in search


def test_review_rows_carry_data_search_with_both_books_and_the_courses():
    pn = panels(page([rec()], reviews()))
    search = re.search(r'<tr [^>]*data-search="([^"]*)"', pn["mismatch"][1]).group(1)

    assert "titlex" in search and "matchedy" in search and "課甲" in search


def test_cart_rows_are_marked_incart_only_when_the_decision_is_above_zero():
    html = page([rec(decided=12, book_key="a", rec_id=1, title="A"), rec(decided=0, book_key="b", rec_id=2, title="B"),
                 rec(book_key="c", rec_id=3, title="C")])
    rows = re.findall(r"<tr [^>]*data-rec=\"\d+\"[^>]*>", html)

    assert [("data-incart=\"1\"" in r) for r in rows] == [True, False, False]


def test_review_rows_carry_data_priority():
    pn = panels(page([rec()], reviews()))

    assert 'data-priority="high"' in pn["notfound"][1]
    assert 'data-priority="none"' in pn["mismatch"][1]


# ---------- 處理欄 ----------

def test_row_button_says_add_or_remove_by_state():
    undecided = panels(page([rec()]))["recs"][1]
    incart = panels(page([rec(decided=3)]))["recs"][1]

    assert re.search(r'<button class="abtn cartbtn"[^>]*data-cart[^>]*>加入採購單</button>', undecided)
    assert re.search(r'<button class="abtn cartbtn"[^>]*data-cart[^>]*>移出採購單</button>', incart)


def test_add_button_is_disabled_when_the_suggestion_is_zero_but_remove_never_is():
    zero = panels(page([rec(copies=0, need=0, priority="skip")]))["recs"][1]
    zero_in_cart = panels(page([rec(copies=0, need=0, priority="skip", decided=2)]))["recs"][1]

    assert re.search(r'<button class="abtn cartbtn"[^>]*disabled', zero)
    assert not re.search(r'<button class="abtn cartbtn"[^>]*disabled', zero_in_cart)


def test_legacy_row_has_no_cart_button():
    assert "data-cart" not in panels(page([rec(book_key=None)]))["recs"][1]


# ---------- 批次列 ----------

def test_every_panel_has_a_batch_bar_with_the_buttons_that_fit_it():
    pn = panels(page([rec()], reviews(), [decided_group()]))
    recs_chunk = pn["recs"][1]
    all_bar = re.search(r'<div class="rbatch rbatch-all">.*?</div>', recs_chunk, re.S).group(0)
    cart_bar = re.search(r'<div class="rbatch rbatch-cart">.*?</div>', recs_chunk, re.S).group(0)

    assert [m for m in re.findall(r'data-pbatch="(\w+)"', all_bar)] == ["suggested", "zero", "clear"]
    assert "加入採購單（採用系統建議）" in all_bar and "設為不買" in all_bar and "清除決定" in all_bar
    assert [m for m in re.findall(r'data-pbatch="(\w+)"', cart_bar)] == ["clear", "zero"]
    assert "移出採購單" in cart_bar and "設為不買" in cart_bar
    assert 'data-batch="matched"' in pn["mismatch"][1] and 'data-batch="none"' in pn["notfound"][1]
    assert 'data-ubatch="undo"' in pn["decided"][1] and "撤銷所選" in pn["decided"][1]


def test_batch_bars_never_offer_buy_original_or_custom_title():
    html = page([rec()], reviews(), [decided_group()])

    assert 'data-batch="original"' not in html and 'data-batch="custom"' not in html


# ---------- 逸出 ----------

def test_untrusted_text_is_escaped_everywhere_in_the_new_cells():
    evil = '"><script>alert(1)</script>'
    html = page([rec(title=evil, book_key=evil)], reviews(), [dataclass_replace_title(evil)])

    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def dataclass_replace_title(evil):
    import dataclasses
    return dataclasses.replace(decided_group(), title=evil, buy_title=evil)
