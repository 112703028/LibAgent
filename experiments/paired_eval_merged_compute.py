"""合併 paired evaluation 兩批（原 120 + 新 180 = 300），重算 transition 表 +
分層加權配對錯誤率 + Wilson 95% CI。

母體比例（來自抽樣時 verified 2372 本：same 1904, diff 468）：same 80.3% / diff 19.7%。
分層加權還原到母體，Wilson CI 用有效樣本量近似。

用法：python experiments/paired_eval_merged_compute.py
"""
import csv
from math import sqrt
from pathlib import Path

_F1 = Path(__file__).resolve().parent / "paired_eval_to_annotate.csv"
_F2 = Path(__file__).resolve().parent / "paired_eval_expand_to_annotate.csv"

_W_SAME, _W_DIFF = 1904 / 2372, 468 / 2372  # 母體比例


def _tf(v):
    return v.strip() in ("t", "對", "是", "TRUE")


def _wrong(v):
    return v.strip() in ("f", "錯", "否", "FALSE")


def _wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return ((c - h) * 100, (c + h) * 100)


def _load(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    keys = list(rows[0].keys())
    gc = next(k for k in keys if "生成正確" in k)
    pc = next(k for k in keys if "配對正確" in k)
    sc = next(k for k in keys if "分層" in k)
    return [(r[gc], r[pc], r[sc].strip()) for r in rows]


def main():
    data = _load(_F1) + _load(_F2)
    n = len(data)

    # transition counts
    cell = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    strat = {"same": [0, 0], "diff": [0, 0]}  # [wrong, total]
    gen_ok = 0
    for g, p, s in data:
        gt, pt = _tf(g), (not _wrong(p))  # p wrong -> False
        pt = _tf(p)
        cell[(gt, pt)] += 1
        if gt:
            gen_ok += 1
        if s in strat:
            strat[s][1] += 1
            if not pt:
                strat[s][0] += 1

    r_same = strat["same"][0] / strat["same"][1] if strat["same"][1] else 0
    r_diff = strat["diff"][0] / strat["diff"][1] if strat["diff"][1] else 0
    weighted = _W_SAME * r_same + _W_DIFF * r_diff

    # 加權後的有效樣本量（用兩層樣本數和近似）：直接用兩層 wrong/total 各自 CI，
    # 再對加權率做保守 CI（用總樣本 n 的 Wilson 近似整體配對錯誤率的分子）
    total_wrong = strat["same"][0] + strat["diff"][0]
    total_dec = strat["same"][1] + strat["diff"][1]

    print(f"=== Paired Evaluation (merged, n={n}) ===\n")
    print("Transition counts:")
    print(f"  gen-correct -> pair-correct : {cell[(True, True)]}")
    print(f"  gen-correct -> pair-wrong   : {cell[(True, False)]}")
    print(f"  gen-wrong   -> pair-correct : {cell[(False, True)]}")
    print(f"  gen-wrong   -> pair-wrong   : {cell[(False, False)]}")
    print(f"  generation correctness: {gen_ok}/{n} = {gen_ok/n*100:.1f}%\n")

    print("Pairing error by stratum:")
    lo_s, hi_s = _wilson(strat["same"][0], strat["same"][1])
    lo_d, hi_d = _wilson(strat["diff"][0], strat["diff"][1])
    print(f"  same: {strat['same'][0]}/{strat['same'][1]} = {r_same*100:.1f}% (CI {lo_s:.1f}-{hi_s:.1f})")
    print(f"  diff: {strat['diff'][0]}/{strat['diff'][1]} = {r_diff*100:.1f}% (CI {lo_d:.1f}-{hi_d:.1f})")

    # 整體（未加權，供對照）+ 母體加權
    lo_u, hi_u = _wilson(total_wrong, total_dec)
    print(f"\nUnweighted pooled: {total_wrong}/{total_dec} = {total_wrong/total_dec*100:.1f}% (CI {lo_u:.1f}-{hi_u:.1f})")
    print(f"Corpus-weighted: {weighted*100:.1f}%")
    print(f"  = {_W_SAME:.3f}*{r_same*100:.1f}% + {_W_DIFF:.3f}*{r_diff*100:.1f}%")


if __name__ == "__main__":
    main()
