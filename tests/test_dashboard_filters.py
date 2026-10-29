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
