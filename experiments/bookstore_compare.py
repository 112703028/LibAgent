"""書商來源比較（只讀，不寫資料庫）：隨機抽 N 本「查無此書」，每一家書商各自完整查一遍（同一份 validator 後備邏輯），
比較命中數、重疊、速度與限流，用來決定 BOOKSTORE_SOURCES 的順序。

用法：python experiments/bookstore_compare.py [N=100] [輸出.csv]
會呼叫各家書商（每家每本約 2–4 秒）與 LLM（判斷是不是同一本），先用小的 N 試。
"""
import csv
import logging
import random
import re
import sys
import time
from pathlib import Path

from library_agent.agents import validator as V
from library_agent.integrations import eslite

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bookstore_dryrun import not_found_citations  # noqa: E402

STORES = ["taaze", "sanmin"]
SEED = 20261108


class Count429(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.n = 0

    def emit(self, record):
        if "429" in record.getMessage():
            self.n += 1


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    out_path = sys.argv[2] if len(sys.argv) > 2 else "bookstore_compare.csv"
    citations = not_found_citations(0)
    random.Random(SEED).shuffle(citations)
    sample = citations[:n]
    print(f"查無此書（不重複書名）{len(citations)} 本，隨機抽 {len(sample)} 本（seed={SEED}）\n", flush=True)

    counter = Count429()
    logging.getLogger("library_agent").addHandler(counter)
    hits: dict[str, dict[str, object]] = {s: {} for s in STORES}
    seconds: dict[str, float] = {}
    four29: dict[str, int] = {}
    original = V._settings.bookstore_sources
    try:
        for store in STORES:
            V._settings.bookstore_sources = store          # 只開這一家，其餘照 validator 的真實後備邏輯
            before, started = counter.n, time.monotonic()
            for i, c in enumerate(sample, 1):
                found = V._bookstore_fallback(c, low_confidence=False)
                hits[store][c.title] = found
                if i % 10 == 0:
                    print(f"  {store}: {i}/{len(sample)}  命中 {sum(1 for f in hits[store].values() if f)}", flush=True)
            seconds[store] = time.monotonic() - started
            four29[store] = counter.n - before
    finally:
        V._settings.bookstore_sources = original
        eslite.close()

    def zh(title: str) -> bool:
        return bool(re.search(r"[一-鿿]", title))

    print("\n===== 結果 =====")
    for store in STORES:
        got = [t for t, f in hits[store].items() if f]
        print(f"{store:7s} 命中 {len(got):3d}/{len(sample)}（{len(got) / len(sample):.0%}）｜"
              f"平均 {seconds[store] / len(sample):.1f} 秒/本｜429 {four29[store]} 次")
    a, b = STORES
    ha = {t for t, f in hits[a].items() if f}
    hb = {t for t, f in hits[b].items() if f}
    print(f"\n兩家都找到 {len(ha & hb)}｜只有 {a} {len(ha - hb)}｜只有 {b} {len(hb - ha)}｜聯集 {len(ha | hb)}"
          f"（{len(ha | hb) / len(sample):.0%}）")
    for label, flt in (("中日文", True), ("英文等", False)):
        sub = [c.title for c in sample if zh(c.title) == flt]
        if sub:
            print(f"  {label} {len(sub):3d} 本：" + "、".join(
                f"{s} {sum(1 for t in sub if hits[s][t])}" for s in STORES)
                + f"、聯集 {sum(1 for t in sub if hits[a][t] or hits[b][t])}")

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["title", "authors", *[f"{s}_hit" for s in STORES], *[f"{s}_title" for s in STORES],
                    *[f"{s}_authors" for s in STORES], *[f"{s}_reason" for s in STORES]])
        for c in sample:
            row = [c.title, "、".join(c.authors)]
            row += [bool(hits[s][c.title]) for s in STORES]
            row += [(hits[s][c.title].canonical_title if hits[s][c.title] else "") for s in STORES]
            row += [("、".join(hits[s][c.title].canonical_authors) if hits[s][c.title] else "") for s in STORES]
            row += [(hits[s][c.title].pairing_reason if hits[s][c.title] else "") for s in STORES]
            w.writerow(row)
    print(f"\n明細：{out_path}（資料庫沒有任何改動）")


if __name__ == "__main__":
    main()
