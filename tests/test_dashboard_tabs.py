"""各分頁統一的版面：分頁列、共用工具列、欄位骨架、批次列、列上的資料屬性、頁首的購物車抽屜。

統一的骨架：每張表第一格是勾選，其次「優先級｜課程｜▸ 書名｜…」，最右一欄是最後的動作（採購建議是「採購冊數」，審核是「處理」）；
每個面板頂端有同一種批次列；工具列（搜尋、篩選、匯出）只有一條，對目前開著的分頁生效。
購物車不是分頁：它是頁首左上角的購物車圖示，點開是整個購物車（全校、不隨學系篩選）的抽屜。
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


def page(recs=None, review=None, decided=None, cart=(0, 0), pending_apply=0):
    """cart＝(書數, 冊數)：全校的購物車合計（頁首購物車圖示上的數字），與頁面上這批書（可能被學系篩選）無關。"""
    recs = [rec()] if recs is None else recs
    data = {"kpis": {"courses": 1, "citations": 1, "verified": 1, "pending": 0, "copies": 5,
                     "copies_final": sum(r.final for r in recs), "cart_books": sum(r.in_cart for r in recs)},
            "priority": {"high": 1}, "status": {"missing": 1}, "recs": recs, "pending": [],
            "review": review or {k: [] for k in KINDS}, "decided": decided or [], "pending_apply": pending_apply,
            "cart": {"books": cart[0], "copies": cart[1]}}
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


def markup(html):
    """只看標記：去掉 <script>（頁面的 JS 裡也有 data-k="cart"、[data-cart] 這類選擇器字串）。"""
    return re.sub(r"<script>.*?</script>", "", html, flags=re.S)


def panels(html):
    """面板開頭標籤的屬性與內容：{data-k: (標籤字串, 內容)}"""
    out = {}
    for chunk in markup(html).split('<div class="rpanel"')[1:]:
        head = chunk[:chunk.index(">")]
        out[re.search(r'data-k="(\w+)"', head).group(1)] = (head, chunk)
    return out


def headers(table_html):
    return [re.sub(r"<[^>]+>", "", th) for th in re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.S)]


def table_of(panel_chunk):
    return re.search(r"<table.*?</table>", panel_chunk, re.S).group(0)


# ---------- 分頁列 ----------

def test_tab_bar_order_has_no_cart_tab():
    html = page([rec(decided=12)], reviews(), [decided_group()])

    assert [t[0] for t in tabs(html)] == ["recs", "mismatch", "notfound", "other", "decided"]
    assert 'data-k="cart"' not in markup(html) and "data-panel=" not in markup(html)
    assert "rbatch-cart" not in markup(html) and 'data-view=' not in markup(html)


def test_the_cart_is_a_header_button_with_a_school_wide_count_and_a_hidden_drawer():
    html = markup(page([rec(decided=12)], reviews(), cart=(3, 40)))

    btn = re.search(r'<button[^>]*id="cartbtn"[^>]*>(.*?)</button>', html, re.S)
    assert btn and 'aria-label="購物車"' in btn.group(0)
    assert re.search(r'id="cartcount"[^>]*>3<', btn.group(0))                    # 全校 3 本，不是頁面上（可能被學系篩選）的書數
    assert html.index('id="cartbtn"') < html.index('class="card"') < html.index('id="review"')   # 在頁首，卡片之前
    drawer = re.search(r'<aside[^>]*id="cartdrawer"[^>]*>', html).group(0)
    assert "hidden" in drawer
    for part in ('id="cartlist"', 'id="carttotal"', 'id="cartexport"', 'id="cartclear"', 'id="cartclose"'):
        assert part in html, part


def test_the_drawer_has_a_hidden_orphan_section_after_the_list():
    html = markup(page([rec()]))

    orph = re.search(r'<div[^>]*id="cartorphans"[^>]*>', html).group(0)
    assert "hidden" in orph and html.index('id="cartlist"') < html.index('id="cartorphans"')

def test_the_cart_button_shows_zero_when_nothing_is_in_the_cart():
    html = markup(page([rec()]))

    assert re.search(r'id="cartcount"[^>]*>0<', html)


def test_review_log_tab_is_named_review_log():
    html = page([rec()], reviews(), [decided_group()])

    label = next(t for t in tabs(html) if t[0] == "decided")[2]
    assert label == "審核紀錄 1 組" and "已決定" not in label


def test_tab_bar_is_present_with_the_purchase_list_even_without_review_items():
    assert [t[0] for t in tabs(page())] == ["recs"]


# ---------- 兩層導覽：採購｜審核 ----------

def top_buttons(html):
    return re.findall(r'<button class="toptab[^"]*" data-top="(\w+)"[^>]*>([^<]*)</button>', markup(html))


def test_two_level_nav_has_a_buy_and_a_review_button_with_their_counts():
    html = page([rec()], reviews(), [decided_group()])

    assert top_buttons(html) == [("buy", "採購 1 本"), ("review", "審核 3 組待處理")]
    assert re.search(r'<button class="toptab active" data-top="buy"', html)          # 預設開在採購


def test_the_top_bar_sits_above_the_sub_tabs_and_the_toolbar():
    html = markup(page([rec()], reviews()))

    assert html.index('class="toplevel"') < html.index('class="filters maintabs"') < html.index('id="toolbar"')


def test_without_review_items_there_is_no_top_bar():
    html = page()

    assert top_buttons(html) == [] and 'class="toplevel"' not in markup(html)


def test_sub_tabs_carry_their_group_and_only_the_open_groups_are_visible():
    html = page([rec()], reviews(), [decided_group()])
    by_k = {k: attrs for k, attrs, _label in tabs(html)}

    assert 'data-top="buy"' in by_k["recs"] and "hidden" not in by_k["recs"]
    for k in ("mismatch", "notfound", "other", "decided"):
        assert 'data-top="review"' in by_k[k] and "hidden" in by_k[k], k
    assert re.search(r'<div class="filters maintabs"[^>]* hidden', markup(html))     # 採購區只有一個分頁，不顯示子分頁列


def test_the_apply_banner_belongs_to_the_review_area_and_the_buy_area_gets_a_pointer():
    html = markup(page([rec()], reviews(), pending_apply=2))

    banner = re.search(r'<div class="rapply"[^>]*>', html).group(0)
    assert 'data-top="review"' in banner and "hidden" in banner                       # 採購區看不到「套用」按鈕
    hint = re.search(r'<div class="applyhint"[^>]*>.*?</div>', html, re.S).group(0)
    assert 'data-top="buy"' in hint and "hidden" not in hint.split(">")[0]
    assert "有 2 筆審核決定尚未套用" in hint and 'data-goto-top="review"' in hint
    assert hint.count("applybtn") == 0                                                  # 提示不是按鈕本身，不能在這裡套用


def test_no_pointer_when_nothing_waits_to_be_applied():
    assert 'class="applyhint"' not in markup(page([rec()], reviews(), pending_apply=0))


def test_without_a_top_bar_the_banner_is_always_visible():
    # 學系篩選時審核紀錄可能是空的，但待套用數是全校的：沒有兩層導覽時橫幅不能被藏起來
    html = markup(page([rec()], pending_apply=3))

    assert 'class="toplevel"' not in html
    assert re.search(r'<div class="rapply">', html) and 'class="applyhint"' not in html


def test_the_ui_says_purchase_suggestions_not_purchase_list():
    html = markup(page([rec()], reviews(), [decided_group()], pending_apply=2))

    assert "採購清單" not in html                       # 「採購清單」容易和「購物車」搞混
    assert "採購單" not in page([rec(decided=2)], reviews(), [decided_group()], cart=(1, 2), pending_apply=2)   # 連 JS 的提示文字都算：統一叫「購物車」
    assert "加入採購建議" in html and "套用後，放行的書才會出現在採購建議" in html


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

    assert headers(table) == ["", "優先級", "課程", "書名", "類別", "館藏狀態", "系統建議", "理由", "採購冊數"]


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

    assert dashboard._PRIORITY_SHORT["high"] in pn["notfound"][1]                 # 查無此書：有採購建議的優先級
    assert "已擋下，不在採購建議" in pn["mismatch"][1]                               # 配錯：被擋下


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

    assert re.search(r'<button class="abtn cartbtn"[^>]*data-cart[^>]*>加入購物車</button>', undecided)
    assert re.search(r'<button class="abtn cartbtn"[^>]*data-cart[^>]*>移出購物車</button>', incart)


def test_add_button_is_disabled_when_the_suggestion_is_zero_but_remove_never_is():
    zero = panels(page([rec(copies=0, need=0, priority="skip")]))["recs"][1]
    zero_in_cart = panels(page([rec(copies=0, need=0, priority="skip", decided=2)]))["recs"][1]

    assert re.search(r'<button class="abtn cartbtn"[^>]*disabled', zero)
    assert not re.search(r'<button class="abtn cartbtn"[^>]*disabled', zero_in_cart)


def test_legacy_row_has_no_cart_button():
    assert "data-cart" not in panels(page([rec(book_key=None)]))["recs"][1]


def test_the_cart_column_keeps_input_and_button_on_one_line_and_never_wraps_the_label():
    css = re.search(r"<style>(.*?)</style>", page(), re.S).group(1)

    btn = re.search(r"\.cartbtn \{([^}]*)\}", css).group(1)
    assert "white-space:nowrap" in btn and "inline-block" in btn                    # 「移出購物車」不能折成兩行，與輸入框同一行
    inp = re.search(r"\.dec-in \{([^}]*)\}", css).group(1)
    assert "inline-block" in inp and re.search(r"width:\d+px", inp)                 # 輸入框固定寬度（放得下「尚未決定」）
    col = re.search(r"td\.dec \{([^}]*)\}", css).group(1)
    assert "white-space:nowrap" in col and int(re.search(r"min-width:(\d+)px", col).group(1)) >= 190   # 放得下同一行

# ---------- 批次列 ----------

def test_purchase_and_log_panels_have_a_batch_bar_and_pending_panels_do_not():
    pn = panels(page([rec()], reviews(), [decided_group()]))
    recs_chunk = pn["recs"][1]
    bars = re.findall(r'<div class="rbatch[^"]*">.*?</div>', recs_chunk, re.S)

    assert len(bars) == 1                                                     # 購物車移到抽屜，只剩一條批次列
    assert [m for m in re.findall(r'data-pbatch="(\w+)"', bars[0])] == ["suggested", "zero", "clear"]
    assert "加入購物車（採用系統建議）" in bars[0] and "設為不買" in bars[0] and "清除決定" in bars[0]
    for kind in ("mismatch", "notfound", "other"):
        assert "data-batch" not in pn[kind][1] and 'class="rbatch"' not in pn[kind][1], kind     # 待審分頁沒有批次列
    assert 'data-ubatch="undo"' in pn["decided"][1] and "撤銷所選" in pn["decided"][1]


# ---------- 逸出 ----------

def test_untrusted_text_is_escaped_everywhere_in_the_new_cells():
    evil = '"><script>alert(1)</script>'
    html = page([rec(title=evil, book_key=evil)], reviews(), [dataclass_replace_title(evil)])

    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def dataclass_replace_title(evil):
    import dataclasses
    return dataclasses.replace(decided_group(), title=evil, buy_title=evil)


# ---------- 前端依賴的結構 ----------

def test_review_and_log_title_cells_wrap_the_title_so_the_export_can_leave_the_authors_out():
    pn = panels(page([rec()], reviews(), [decided_group()]))

    assert '<span class="bt">TitleX</span>' in pn["mismatch"][1] and '<span class="bm">MatchedY</span>' in pn["mismatch"][1]
    assert '<span class="bt">DONE</span>' in pn["decided"][1]


def test_the_front_end_is_wired_to_the_cart_batch_and_undo_actions():
    html = page([rec()], reviews(), [decided_group()])

    for name in ("_cartToggle", "_purchaseBatch", "_undoBatch", "_selectTab", "_applyRecFilters", "_refreshCart",
                 "_openCart", "_loadCart", "_renderCart", "_withRow", "_selectTop", "_syncTop"):
        assert f"function {name}(" in html or f"async function {name}(" in html, name
    assert html.count("addEventListener('click', _onTraceClick)") == 1       # 展開鈕的事件只綁一次
