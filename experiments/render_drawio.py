"""把 .drawio 畫成 PNG（README 用）。用 draw.io 官方的網頁檢視器在本機的 Edge 裡渲染，不需要安裝 draw.io 桌面版。

用法：python experiments/render_drawio.py docs/images/architecture.drawio [輸出.png] [倍率=2]

檢視器的腳本（viewer-static.min.js）會從 viewer.diagrams.net 載入，所以需要網路；圖的內容只在本機瀏覽器裡渲染，
不會傳給任何服務。需要 Playwright 與已安裝的 Edge（channel="msedge"）。要編輯圖請用 diagrams.net 或 draw.io 開 .drawio。
"""
import html
import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWER = "https://viewer.diagrams.net/js/viewer-static.min.js"
PAPER = "#F1F2EE"


def render(src: Path, out: Path, scale: float = 2.0) -> None:
    config = {"highlight": "#0000ff", "nav": False, "resize": False, "toolbar": "", "edit": "_blank", "xml": src.read_text(encoding="utf-8")}
    page_html = (f'<!doctype html><meta charset="utf-8"><body style="margin:0;background:{PAPER}">'
                 f'<div id="frame" style="padding:16px;background:{PAPER}">'          # 要有寬度：寬度 0 的容器檢視器不會畫
                 f'<div class="mxgraph" data-mxgraph="{html.escape(json.dumps(config), quote=True)}"></div></div>'
                 f'<script src="{VIEWER}"></script></body>')
    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "view.html"
        page_file.write_text(page_html, encoding="utf-8")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 900}, device_scale_factor=scale)
            page.goto(page_file.as_uri(), wait_until="load", timeout=60000)      # 檢視器在 load 之後才非同步畫圖
            page.wait_for_selector("#frame svg", state="attached", timeout=30000)
            page.wait_for_timeout(800)                                   # 等字型與文字排版穩定
            box = page.locator("#frame svg").first.bounding_box()
            pad = 20
            page.screenshot(path=str(out), clip={"x": max(box["x"] - pad, 0), "y": max(box["y"] - pad, 0),
                                                 "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad})
            browser.close()


if __name__ == "__main__":
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".png")
    render(src, out, float(sys.argv[3]) if len(sys.argv) > 3 else 2.0)
    print("wrote", out)
