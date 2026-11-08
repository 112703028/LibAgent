"""誠品：書商後備驗證的第二家（要用瀏覽器）。

搜尋頁背後的 JSON 接口是 holmes.eslite.com/v1/search（書名、作者、出版社、出版日、isbn10、is_book）。
一般 HTTP 請求會被 Cloudflare 回 403，所以用 Playwright 開這台電腦已安裝的 Edge（channel="msedge"，不下載瀏覽器），
先載入誠品首頁，之後在頁面裡用 fetch() 呼叫這個接口。

Playwright 的同步 API 不能跨執行緒使用，而 validator 有多個 worker 執行緒：所有查詢都交給模組內
同一條專用執行緒（max_workers=1 的 executor），瀏覽器在那條執行緒裡延遲啟動、重複使用。
任何失敗（找不到 Edge、啟動失敗、被擋、逾時）都記一行 log、回空清單，並在這次執行內停用誠品；
validator 結束時呼叫 close()，關閉瀏覽器並解除停用。

2026-11-08 實測：誠品的 Cloudflare 對「誠實的」無頭 Edge（User-Agent 含 HeadlessChrome）直接回 403
（頁面標題 "Attention Required"），所以目前實際上查不到東西；本模組遇到這種情況就是「停用、回空清單」。
試驗時 API Anything 能通，是因為它把 User-Agent 的 "HeadlessChrome" 改寫成 "Chrome"、讓網站認不出是無頭瀏覽器——
那是繞過網站的存取限制，這裡**不這麼做**。所以預設的 BOOKSTORE_SOURCES 只有 taaze；
要不要啟用誠品（例如改成使用者自己看得到的 Edge 視窗），由使用者決定。
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from library_agent.book_identity import normalize_isbn
from library_agent.integrations.bookstore import Pacer, StoreRecord, split_authors

log = logging.getLogger(__name__)

HOME_URL = "https://www.eslite.com/"
API_URL = "https://holmes.eslite.com/v1/search"
_PACER = Pacer(min_interval=2.0)

# 在誠品的頁面裡呼叫接口：參數以物件傳入、用 URLSearchParams 組（不手拼字串）
_FETCH_JS = """async ({url, params}) => {
  const u = new URL(url);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  const r = await fetch(u.toString());
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}"""

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_session = None            # 只在誠品專用執行緒裡讀寫
_disabled = False


class _EdgeSession:
    """一個開著誠品首頁的 Edge（headless）。只能在建立它的那條執行緒裡使用。"""

    def __init__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(channel="msedge", headless=True)
            self._page = self._browser.new_page()
            self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            self._pw.stop()
            raise

    def fetch_json(self, params: dict) -> dict:
        return self._page.evaluate(_FETCH_JS, {"url": API_URL, "params": params})

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def _launch_browser():
    """啟動瀏覽器工作階段（測試以假的取代）。回傳的物件要有 fetch_json(params) 與 close()。"""
    return _EdgeSession()


def parse_search_json(data: dict) -> list[StoreRecord]:
    records = []
    for r in (data or {}).get("results") or []:
        if r.get("is_book") != "yes" or not r.get("name"):
            continue
        authors = [a for raw in (r.get("authors") or []) for a in split_authors(str(raw))]
        publishers = r.get("manufacturers") or []
        records.append(StoreRecord(
            title=str(r["name"]).strip(),
            authors=authors,
            isbn_13=normalize_isbn(r.get("isbn10")),
            publisher=str(publishers[0]) if publishers else "",
            published=str(r.get("publish_time") or "")[:10],
            source="eslite",
        ))
    return records


def _executor_now() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eslite")
        return _executor


def _fetch(params: dict) -> dict:
    """在誠品專用執行緒裡執行。"""
    global _session
    if _session is None:
        _session = _launch_browser()
    return _session.fetch_json(params)


def search(query: str, timeout: float = 30.0) -> list[StoreRecord]:
    global _disabled
    if _disabled:
        return []
    try:
        _PACER.wait()
        future = _executor_now().submit(_fetch, {"q": query, "page_size": "20", "page_no": "1"})
        return parse_search_json(future.result(timeout=timeout))
    except Exception as e:  # noqa: BLE001 — 誠品出錯一律當作沒找到，並在這次執行內停用
        log.warning("誠品搜尋失敗，這次執行不再使用誠品（%s）：%r", query[:40], e)
        _disabled = True
        return []


def _shutdown_session() -> None:
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception as e:  # noqa: BLE001
            log.warning("關閉誠品瀏覽器失敗：%r", e)
        _session = None


def close() -> None:
    """關閉瀏覽器與專用執行緒，並解除停用（下一次執行會再試誠品）。可重複呼叫。"""
    global _executor, _disabled
    with _lock:
        ex, _executor = _executor, None
    if ex is not None:
        try:
            ex.submit(_shutdown_session).result(timeout=10)
        except Exception as e:  # noqa: BLE001 — 卡住的查詢還在跑時，不為了關閉而等下去
            log.warning("等待誠品瀏覽器關閉逾時：%r", e)
        ex.shutdown(wait=False)
    _disabled = False
