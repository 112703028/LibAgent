"""書商後備驗證（TAAZE、誠品）共用的資料類別與工具。

書商的搜尋結果只是「相關書目」，是不是同一本由 match_auditor.pick_same_book 判斷；
這裡只負責把各家的結果整理成同一種格式，以及控制每一家的請求速度。
"""
import random
import re
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class StoreRecord:
    title: str
    authors: list[str]
    isbn_13: str | None
    publisher: str
    published: str        # 出版日期字串（YYYY-MM-DD 或書商給的原樣）
    source: str           # "taaze" | "eslite"


class Pacer:
    """同一家書商的請求限速：執行緒安全，兩次 wait() 返回的間隔至少 min_interval × 隨機 0.7–1.3。
    validator 有多個 worker 執行緒，各自查同一家時也會排隊。"""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_interval * random.uniform(0.7, 1.3)
        if start > now:
            time.sleep(start - now)


_SEPARATORS = re.compile(r"\s*(?:——|──|—|–|:|：| - |/|／)\s*")


def main_title(title: str) -> str | None:
    """主書名：第一個分隔符號前的部分（去掉副標題、斜線後的外文書名）。
    與原書名相同（沒有可切的）或少於 2 個字時回 None（不必補查）。"""
    title = (title or "").strip()
    head = _SEPARATORS.split(title, maxsplit=1)[0].strip()
    if not head or head == title or len(head) < 2:
        return None
    return head


_AUTHOR_PREFIX = re.compile(r"^\s*作者\s*[:：]\s*")
_AUTHOR_SEP = re.compile(r"[、,，/／;；]")


def split_authors(raw: str) -> list[str]:
    """「作者：岸見一郎、古賀史健」「岸見一郎/ 古賀史健」→ 個別作者。"""
    raw = _AUTHOR_PREFIX.sub("", raw or "")
    return [a.strip() for a in _AUTHOR_SEP.split(raw) if a.strip()]
