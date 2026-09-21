"""讀回人工標註的 paired evaluation，算 transition 表 + 母體加權配對錯誤率。

回應審稿人 major concern #1（paired evaluation）+ sampling 加權。
同一批書追蹤「生成正確」×「配對正確」，用母體真實比例（same/diff）加權還原。

用法：python experiments/paired_eval_analyze.py
"""
import csv
from pathlib import Path

_IN = Path(__file__).resolve().parent / "paired_eval_to_annotate.csv"
_OUT = Path(__file__).resolve().parent / "paired_eval_result.csv"

# 母體比例（來自 paired_eval_sample.py 抽樣時的母體：same 1904, diff 468）
_W = {"same": 1904 / 2372, "diff": 468 / 2372}


def _tf(v: str):
    v = (v or "").strip()
    if v in ("t", "對", "是", "TRUE", "True"):
        return True
    if v in ("f", "錯", "否", "FALSE", "False"):
        return False
    return None


def main():
    rows = list(csv.DictReader(open(_IN, encoding="utf-8-sig")))
    keys = list(rows[0].keys())
    c_gen = next(k for k in keys if "生成正確" in k)
    c_pair = next(k for k in keys if "配對正確" in k)
    c_strat = next(k for k in keys if "分層" in k)

    # transition counts（未加權）
    cell = {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    strat = {"same": [0, 0], "diff": [0, 0]}  # [配對錯, 總數]
    for r in rows:
        g, p = _tf(r[c_gen]), _tf(r[c_pair])
        if g is None or p is None:
            continue
        cell[(g, p)] += 1
        s = r[c_strat].strip()
        strat[s][1] += 1
        if p is False:
            strat[s][0] += 1

    r_same = strat["same"][0] / strat["same"][1]
    r_diff = strat["diff"][0] / strat["diff"][1]
    weighted = _W["same"] * r_same + _W["diff"] * r_diff

    n = sum(cell.values())
    gen_ok = cell[(True, True)] + cell[(True, False)]

    lines = [
        "=== Paired Evaluation Result ===",
        f"total annotated: {n}",
        "",
        "-- Transition counts (unweighted) --",
        f"gen-correct -> pair-correct : {cell[(True, True)]}",
        f"gen-correct -> pair-wrong   : {cell[(True, False)]}",
        f"gen-wrong   -> pair-correct : {cell[(False, True)]}",
        f"gen-wrong   -> pair-wrong   : {cell[(False, False)]}",
        f"generation correctness: {gen_ok}/{n} = {gen_ok/n*100:.1f}%",
        "",
        "-- Pairing error rate by stratum --",
        f"same: {strat['same'][0]}/{strat['same'][1]} = {r_same*100:.1f}%",
        f"diff: {strat['diff'][0]}/{strat['diff'][1]} = {r_diff*100:.1f}%",
        "",
        "-- Corpus-weighted pairing error rate --",
        f"weights: same {_W['same']*100:.1f}%, diff {_W['diff']*100:.1f}%",
        f"= {_W['same']:.3f}*{r_same*100:.1f}% + {_W['diff']:.3f}*{r_diff*100:.1f}%"
        f" = {weighted*100:.1f}%",
    ]
    report = "\n".join(lines)
    print(report)

    with open(_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerow(["n", n])
        w.writerow(["gen_correct->pair_correct", cell[(True, True)]])
        w.writerow(["gen_correct->pair_wrong", cell[(True, False)]])
        w.writerow(["gen_wrong->pair_correct", cell[(False, True)]])
        w.writerow(["gen_wrong->pair_wrong", cell[(False, False)]])
        w.writerow(["generation_correctness_pct", round(gen_ok / n * 100, 1)])
        w.writerow(["same_pairing_error_pct", round(r_same * 100, 1)])
        w.writerow(["diff_pairing_error_pct", round(r_diff * 100, 1)])
        w.writerow(["corpus_weighted_error_pct", round(weighted * 100, 1)])
    print(f"\n寫入 {_OUT}")


if __name__ == "__main__":
    main()
