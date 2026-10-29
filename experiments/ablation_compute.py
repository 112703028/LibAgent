"""ablation 最終計算：讀 ablation_full_to_annotate.csv 的人工標，
算 current / no_author / reject 三變體的配對錯誤率（中文 NCL 92 本）。

不重查 NCL——直接用待標檔記錄的候選 + 人工標，避免候選隨時間變動的對不齊。

用法：python experiments/ablation_compute.py
"""
import csv
from pathlib import Path

_IN = Path(__file__).resolve().parent / "ablation_full_to_annotate.csv"
_OUT = Path(__file__).resolve().parent / "ablation_result.csv"


def _tf(v):
    return v.strip() in ("t", "對", "是")


def _wrong(v):
    return v.strip() in ("f", "錯", "否")


def main():
    rows = list(csv.DictReader(open(_IN, encoding="utf-8-sig")))
    keys = list(rows[0].keys())
    cc = next(k for k in keys if k.startswith("標_current"))
    nc = next(k for k in keys if k.startswith("標_no_author"))
    rc = next(k for k in keys if k.startswith("標_reject"))

    cur_c = sum(1 for r in rows if _tf(r[cc]))
    cur_w = sum(1 for r in rows if _wrong(r[cc]))
    na_c = sum(1 for r in rows if _tf(r[nc]))
    na_w = sum(1 for r in rows if _wrong(r[nc]))

    rej_c = rej_w = rej_reject = 0
    for r in rows:
        v = r[rc].strip()
        if v == "查無":
            rej_reject += 1
        elif v == "同":
            if _tf(r[cc]):
                rej_c += 1
            elif _wrong(r[cc]):
                rej_w += 1
        elif _tf(r[rc]):
            rej_c += 1
        elif _wrong(r[rc]):
            rej_w += 1

    def rate(w, c):
        return w / (w + c) * 100 if (w + c) else 0

    n = len(rows)
    variants = [
        ("current", cur_c, cur_w, 0),
        ("no_author", na_c, na_w, 0),
        ("reject", rej_c, rej_w, rej_reject),
    ]
    print(f"NCL Chinese, n={n}\n")
    print(f"{'variant':12}{'correct':>9}{'wrong':>7}{'reject':>8}{'error%':>9}")
    for name, c, w, rej in variants:
        print(f"{name:12}{c:>9}{w:>7}{rej:>8}{rate(w, c):>8.1f}%")

    with open(_OUT, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["variant", "correct", "wrong", "reject", "error_pct"])
        for name, c, w, rej in variants:
            wr.writerow([name, c, w, rej, round(rate(w, c), 1)])
    print(f"\n→ {_OUT}")


if __name__ == "__main__":
    main()
