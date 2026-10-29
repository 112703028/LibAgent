"""把 pipeline 的終端文字輸出搬到網頁：print() / stderr 多存一份，按行記在記憶體裡。

流程：按「執行」時 install()，把 sys.stdout / sys.stderr 換成複製器 _Tee——字照樣印到終端機，
同時按行記進 _buffer；dashboard 的 GET /log 用 read() 取「流水號大於 N 的新行」，前端接在
文字框後面。pipeline 結束就 uninstall() 還原。

過濾不用寫規則：uvicorn 的 `INFO: ... GET /status` 存取日誌走它自己持有的輸出管道，
不經過 sys.stdout；複製器是在伺服器啟動「之後」才換上去的，所以只會收到 pipeline 的 print。
（已實測：複製器記到 3 行 print、0 行存取日誌，終端機兩種都照樣看得到。）

每一行會加上 `[節點名稱] ` 前綴，節點名稱來自 graph._with_tracking 的 bind_node()。
"""
import sys
import threading
import time
from contextvars import ContextVar, Token

MAX_LINES = 2000   # 記憶體裡最多保留幾行；超過就丟最舊的
_MAX_LINE = 500    # 單行最長字元數（只截 log，不影響終端機）

_lock = threading.Lock()
_buffer: list[tuple[int, str]] = []   # (流水號, 文字)
_seq = 0
# epoch 代表「第幾次執行」。每次 reset() 都會變，且以毫秒時間為底，所以伺服器重啟後也不會
# 跟重啟前的值撞到——否則前端拿著舊游標會永遠漏掉新行。
_epoch = int(time.time() * 1000)

# 目前這個執行環境綁定的節點。ThreadPoolExecutor 的 worker 不會繼承它，
# 那些執行緒印的行改用 _running 裡「最近開始、還在跑」的節點。
_node: ContextVar[str | None] = ContextVar("runlog_node", default=None)
_running: list[str] = []

_installed: tuple["_Tee", "_Tee", object, object] | None = None


def _append(line: str) -> None:
    if not line.strip():
        return
    global _seq
    with _lock:
        node = _node.get() or (_running[-1] if _running else None)
        text = (f"[{node}] {line}" if node else line)[:_MAX_LINE]
        _seq += 1
        _buffer.append((_seq, text))
        del _buffer[:-MAX_LINES]


class _Tee:
    """檔案物件的外殼：先記下完整的行，再原樣寫給真正的輸出。"""

    def __init__(self, real):
        self._real = real
        # 每個執行緒各自累積「還沒換行的半行」：print 會把文字與換行分兩次 write，
        # 多個 worker 同時印時共用一個緩衝會把不同行接在一起。
        self._local = threading.local()

    def write(self, s):
        pending = getattr(self._local, "partial", "") + s
        *done, self._local.partial = pending.split("\n")
        # 先記再寫：就算真正的輸出寫失敗（例如終端機編碼），這一行也不會漏掉
        for line in done:
            _append(line)
        return self._real.write(s)

    def __getattr__(self, name):   # flush / isatty / encoding / fileno… 都交給真正的輸出
        return getattr(self._real, name)


def install() -> None:
    """把 stdout / stderr 換成複製器。重複呼叫無效。"""
    global _installed
    with _lock:
        if _installed is not None:
            return
        out, err = sys.stdout, sys.stderr
        t_out, t_err = _Tee(out), _Tee(err)
        sys.stdout, sys.stderr = t_out, t_err
        _installed = (t_out, t_err, out, err)


def uninstall() -> None:
    """還原 stdout / stderr。只還原「現在還是我們的複製器」的那一條，避免蓋掉別人後來換上的輸出。"""
    global _installed
    with _lock:
        if _installed is None:
            return
        t_out, t_err, out, err = _installed
        _installed = None
        if sys.stdout is t_out:
            sys.stdout = out
        if sys.stderr is t_err:
            sys.stderr = err


def reset() -> None:
    """新的一次執行開始：清空 log，並換一個 epoch 讓前端知道要整份重拿。流水號繼續遞增。"""
    global _epoch
    with _lock:
        _buffer.clear()
        _epoch = max(_epoch + 1, int(time.time() * 1000))


def bind_node(name: str) -> Token:
    with _lock:
        _running.append(name)
    return _node.set(name)


def unbind_node(token: Token) -> None:
    name = _node.get()
    _node.reset(token)
    with _lock:
        if name in _running:
            _running.remove(name)


def current_node() -> str | None:
    """目前這個執行環境綁定的節點（只看 contextvar，不含 worker 的退而求其次）。"""
    return _node.get()


def read(after: int = 0, epoch: int | None = None) -> dict:
    """回傳流水號大於 after 的行。epoch 與目前不同（前端手上是上一次執行的游標）就整份重給。"""
    with _lock:
        if epoch is not None and epoch != _epoch:
            after = 0
        return {
            "epoch": _epoch,
            "last": _seq,
            "lines": [text for n, text in _buffer if n > after],
        }
