"""dashboard 的執行紀錄（終端文字 log）：取代 mermaid 流程圖之後的頁面行為。"""
from library_agent import dashboard


def test_render_has_no_mermaid_or_static():
    html = dashboard._render(dashboard._load_data())

    assert "mermaid" not in html
    assert "/static/" not in html


def test_app_has_no_static_mount():
    # /static 目錄只剩 mermaid.min.js，檔案刪除後目錄不存在，掛載會讓 import 失敗
    assert "/static" not in [getattr(r, "path", None) for r in dashboard.app.routes]
