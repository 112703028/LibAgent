"""完整重標 ablation（中文 NCL 92 本）：重跑 current/no_author/reject 三變體，
把各變體挑到的候選『完整書名』寫進待標檔。你標各變體對錯後，算 ablation 時
直接讀檔（不重查 NCL），徹底避免候選隨時間變動導致的對不齊。

三變體常挑到同一筆 → 「同current」標記，同一候選只需標一次。
reject 回查無 → 標記「查無」不用標。

用法：python experiments/ablation_full_annotate.py
輸出：ablation_full_to_annotate.csv
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from library_agent.integrations import nla

_PAIRED = Path(__file__).resolve().parent / "paired_eval_to_annotate.csv"
_OUT = Path(__file__).resolve().parent / "ablation_full_to_annotate.csv"


def _norm(s):
    return re.sub(r"[^a-z0-9一-鿿]", "", (s or "").lower())


def pick_reject(records, authors):
    if not records:
        return None
    qn = [nla._normalize_author(a) for a in (authors or []) if a and a.strip()]
    if not qn:
        return None
    for rec in records:
        cn = [nla._normalize_author(a) for a in rec.canonical_authors]
        for q in qn:
            for c in cn:
                if q and c and (q in c or c in q):
                    return rec
    return None


def main():
    paired = list(csv.DictReader(open(_PAIRED, encoding="utf-8-sig")))
    pk = list(paired[0].keys())

    def pg(r, n):
        return r[next(k for k in pk if n in k)]

    ncl = [r for r in paired if pg(r, "來源").strip() == "ncl"]

    out = []
    for r in ncl:
        title = pg(r, "原始書名")
        authors = [a.strip() for a in re.split(r"[/\s]+", pg(r, "原始作者")) if a.strip()]
        recs = nla._query(f'alma.title="{title}"')
        if not recs:
            clean = nla._clean_title(title)
            if clean and clean != title:
                recs = nla._query(f'alma.title="{clean}"')

        cur = nla._pick_best_match(recs, authors)
        na = recs[0] if recs else None
        rej = pick_reject(recs, authors)

        cur_t = cur.canonical_title if cur else "(NOT FOUND)"
        na_t = na.canonical_title if na else "(NOT FOUND)"
        na_same = na is not None and _norm(na_t) == _norm(cur_t)
        rej_same = rej is not None and _norm(rej.canonical_title) == _norm(cur_t)

        out.append({
            "原始書名": title,
            "原始作者": pg(r, "原始作者"),
            "current挑到": cur_t,
            "標_current(對/錯)": "",
            "no_author挑到": "(同current)" if na_same else na_t,
            "標_no_author(對/錯,同則免)": "同" if na_same else "",
            "reject挑到": "(查無)" if rej is None else ("(同current)" if rej_same else rej.canonical_title),
            "標_reject(對/錯,同/查無則免)": ("查無" if rej is None else ("同" if rej_same else "")),
        })

    with open(_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    n = len(out)
    n_na = sum(1 for o in out if o["標_no_author(對/錯,同則免)"] == "")
    n_rej = sum(1 for o in out if o["標_reject(對/錯,同/查無則免)"] == "")
    print(f"共 {n} 本（中文 NCL）→ {_OUT}")
    print("你需標的欄位：")
    print(f"  『標_current』: 全部 {n} 本")
    print(f"  『標_no_author』（不同current才需）: {n_na} 本")
    print(f"  『標_reject』（不同current、非查無才需）: {n_rej} 本")
    print(f"→ 實際新判斷約 {n + n_na + n_rej} 格")


if __name__ == "__main__":
    main()
