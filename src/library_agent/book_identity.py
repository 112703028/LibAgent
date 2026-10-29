"""書的識別鍵 book_key：決定「兩筆已驗證的書是不是同一本」，供 recommender 把同一本書的多筆建議合併。

規則（設計文件第 4 點）：
  1. 有效 ISBN（過檢查碼；ISBN-10 轉成 13 碼）→ `isbn:<13 碼>`。不同的有效 ISBN 不合併（版次分開）。
  2. 沒有有效 ISBN → 依正規化書名分桶；桶內書名與作者都相同者直接同組，其餘交給判斷函式。
     判斷失敗或出任何意外都「不合併」。
  3. 有有效 ISBN 的書與沒有 ISBN 的同名書不合併（無法判斷是哪個版次）。

為什麼要先過檢查碼：isbn_13 欄位實測有 17 筆不是 ISBN（Alma 記錄編號、格式錯誤的 Google Books 值），
其中兩本作者不同的《經濟學》共用同一個 Alma 編號，照「ISBN 相同就是同一本」會被錯併。
"""
import re
from typing import Callable

from library_agent.agents.match_auditor import _llm_same_book, _norm

_MAX_TITLE_KEY = 450   # book_key 欄位是 VARCHAR(500)，留空間給前綴與 #n 後綴


def _isbn13_check_digit(first12: str) -> int:
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(first12))
    return (10 - total % 10) % 10


def normalize_isbn(raw: str | None) -> str | None:
    """有效的 ISBN-13 回 13 碼；有效的 ISBN-10 轉成 13 碼；其他（含空、Alma 編號）回 None。"""
    if not raw:
        return None
    digits = re.sub(r"[-\s]", "", raw).upper()
    if re.fullmatch(r"\d{13}", digits):
        # 檢查碼之外再要求 978/979 開頭：13 位數字純靠檢查碼有 10% 的機率碰巧通過
        ok = digits.startswith(("978", "979")) and _isbn13_check_digit(digits[:12]) == int(digits[12])
        return digits if ok else None
    if re.fullmatch(r"\d{9}[\dX]", digits):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        if total % 11 != 0:
            return None
        core = "978" + digits[:9]
        return core + str(_isbn13_check_digit(core))
    return None


def _author_set(rec: dict) -> frozenset[str]:
    return frozenset(_norm(a) for a in rec.get("authors") or [] if a)


def _title_key(norm_title: str, cluster_index: int) -> str:
    base = f"title:{norm_title[:_MAX_TITLE_KEY]}"
    return base if cluster_index == 0 else f"{base}#{cluster_index + 1}"


def assign_book_keys(records: list[dict], judge: Callable[[dict, dict], bool]) -> dict[int, str]:
    """records 每筆含 id / isbn / title / authors；回傳 id → book_key。依 id 排序處理，結果與輸入順序無關。"""
    keys: dict[int, str] = {}
    clusters: dict[str, list[dict]] = {}   # 正規化書名 → 各群的代表記錄

    for rec in sorted(records, key=lambda r: r["id"]):
        isbn = normalize_isbn(rec.get("isbn"))
        if isbn:
            keys[rec["id"]] = f"isbn:{isbn}"
            continue
        norm_title = _norm(rec.get("title") or "")
        if not norm_title:
            keys[rec["id"]] = f"rec:{rec['id']}"   # 沒有書名可比，不合併
            continue

        reps = clusters.setdefault(norm_title, [])
        for index, rep in enumerate(reps):
            if _author_set(rep) == _author_set(rec) or _safe_judge(judge, rep, rec):
                keys[rec["id"]] = _title_key(norm_title, index)
                break
        else:
            reps.append(rec)
            keys[rec["id"]] = _title_key(norm_title, len(reps) - 1)
    return keys


def _safe_judge(judge: Callable[[dict, dict], bool], a: dict, b: dict) -> bool:
    """判斷函式出任何意外都當作「不同本」：寧可不合併，也不要讓整個 recommender 崩潰或誤併。"""
    try:
        return bool(judge(a, b))
    except Exception:  # noqa: BLE001
        return False


def llm_judge(client, model: str) -> Callable[[dict, dict], bool]:
    """重用 match_auditor 的判斷（規則已內建在 prompt、作者別名、關於該書的出版物算不同本…），
    但失敗時判「不同本」（與稽核時相反）。"""
    def judge(a: dict, b: dict) -> bool:
        same, _reason = _llm_same_book(
            client, model, a["title"], a.get("authors") or [], b["title"], b.get("authors") or [],
            fail_as_same=False,
        )
        return same
    return judge
