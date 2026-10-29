"""比較三種「原書 vs 驗證器配到的記錄是否同一本」判斷方法，對照 300 筆人工標註。

用途：validator 配對之後想加一道稽核（判斷配到的記錄跟原書是不是同一本，不是就送人工審核），
先量哪種方法最可靠，再決定要不要接進 validator。不改 nla 的配對邏輯。

三種方法：
  rule  字串規則：書名正規化（全形/大小寫/標點、去副標）後，核心書名互相包含即判同一本；
        否則用字元 Jaccard 當分數。當最低底線。
  laya  Laya Noul（是/否機率），state 用 dict 格式（冒煙測試中比文字格式準）。
  llm   系統原本的 LLM（.env 的 LLM_MODEL_MINI），回傳 JSON 的同一本機率。

Ground truth：paired_eval_to_annotate.csv（120）+ paired_eval_expand_to_annotate.csv（180）
的「標註_配對正確」欄，t = 同一本、f = 不同本。

結果逐筆存到 same_book_judge_results.csv，可中斷後續跑（已算過的不重算，--rerun 才重算）。

用法：
    python experiments/same_book_judge_compare.py                 # 三種都跑，300 筆
    python experiments/same_book_judge_compare.py --limit 20      # 先試 20 筆
    python experiments/same_book_judge_compare.py --methods rule,llm
    python experiments/same_book_judge_compare.py --rerun         # 忽略快取全部重算
    python experiments/same_book_judge_compare.py --rerun llm     # 只重算 LLM
"""
import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_DIR = Path(__file__).resolve().parent
_INPUTS = [_DIR / "paired_eval_to_annotate.csv", _DIR / "paired_eval_expand_to_annotate.csv"]
_RESULTS = _DIR / "same_book_judge_results.csv"
_SUMMARY = _DIR / "same_book_judge_summary.txt"

# 母體比例（paired 抽樣時 verified 2372 本：same 1904、diff 468），用來還原母體加權準確率
_W = {"same": 1904 / 2372, "diff": 468 / 2372}

METHODS = ("rule", "laya", "llm")
FIELDS = ["citation_id", "stratum", "orig_title", "orig_authors", "matched_title",
          "matched_authors", "gold_same", "rule_p", "laya_p", "llm_same", "llm_p", "llm_reason"]


# ── 資料 ──────────────────────────────────────────────────────────────

def load_rows() -> list[dict]:
    rows = []
    for path in _INPUTS:
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                def col(name):
                    return r[next(k for k in r if name in k)].strip()

                label = col("配對正確")
                if label not in ("t", "f", "對", "錯"):
                    continue
                rows.append({
                    "citation_id": col("citation_id"),
                    "stratum": col("分層"),
                    "orig_title": col("原始書名"),
                    "orig_authors": col("原始作者"),
                    "matched_title": col("配到的書名"),
                    "matched_authors": col("配到的作者"),
                    "gold_same": label in ("t", "對"),
                })
    return rows


def load_cache() -> dict[str, dict]:
    if not _RESULTS.exists():
        return {}
    with open(_RESULTS, encoding="utf-8-sig") as f:
        return {r["citation_id"]: r for r in csv.DictReader(f)}


def save_results(rows: list[dict]) -> Path:
    """寫入結果；若主檔被 Excel 等程式鎖住，改寫備用檔名，避免整批結果白跑。"""
    targets = [_RESULTS, _RESULTS.with_name(f"{_RESULTS.stem}_{int(time.time())}.csv")]
    for path in targets:
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in FIELDS})
            if path != _RESULTS:
                print(f"  !! {_RESULTS.name} 被佔用（Excel 開著？），改存 {path.name}")
            return path
        except PermissionError:
            continue
    raise PermissionError(f"無法寫入 {_RESULTS}，且備用檔名也失敗")


# ── 方法 1：字串規則 ────────────────────────────────────────────────────

try:
    from opencc import OpenCC  # 有裝就做繁簡折疊，沒裝就略過

    _t2s = OpenCC("t2s").convert
except Exception:  # noqa: BLE001
    _t2s = None


_SYMBOL_EQUIV = [
    (r"&", " and "),        # Statistics for Business & Economics == ... and ...
    (r"＆", " and "),
    (r"[''`´]", ""),        # ACSM's == ACSMs
    (r"[­‐-―]", "-"),  # 各種破折號/連字號統一
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    for pat, rep in _SYMBOL_EQUIV:
        s = re.sub(pat, rep, s)
    if _t2s:
        s = _t2s(s)
    # 保留拉丁字母（含附加符號）、數字、CJK；其餘（標點、空白）去除
    return "".join(c for c in s if c.isalnum())


def _core(title: str) -> str:
    return _norm(re.split(r"[:：=＝]", title or "", maxsplit=1)[0])


def _bigrams(s: str) -> set[str]:
    """連續兩字元組成的集合；讓順序有意義（「上海」≠「海上」）。"""
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def rule_score(r: dict) -> float:
    a, b = _core(r["orig_title"]), _core(r["matched_title"])
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / len(ga | gb)


# ── 方法 2：Laya ───────────────────────────────────────────────────────

_LAYA_QUESTION = {
    "same_book": {
        "type": "noul",
        "instructions": (
            "Do the ORIGINAL citation and the MATCHED catalog record refer to the same book? "
            "Ignore traditional/simplified Chinese differences, subtitles, and edition notes."
        ),
    }
}


def run_laya(todo: list[dict]) -> None:
    from laya import Router

    router = Router()
    t0 = time.time()
    for i, r in enumerate(todo, 1):
        state = {
            "original_title": r["orig_title"],
            "original_authors": r["orig_authors"] or "(none)",
            "matched_title": r["matched_title"],
            "matched_authors": r["matched_authors"] or "(none)",
        }
        res = router.predict(state, _LAYA_QUESTION)
        r["laya_p"] = round(float(res["answers"]["same_book"]["noul"]), 4)
        if i % 25 == 0 or i == len(todo):
            print(f"  laya {i}/{len(todo)}  ({time.time() - t0:.0f}s)", flush=True)


# ── 方法 3：LLM ────────────────────────────────────────────────────────

_LLM_SYSTEM = """你是圖書館編目專家。判斷「原始書目」與「書目資料庫配到的記錄」是否指向同一本書（同一部作品）。

視為同一本：
- 繁簡體、異體字、全半形、標點差異
- 有無副標題、版次/增訂版/不同出版年、平裝精裝
- 作者寫法不同（姓名順序、譯名、多列或少列共同作者）

視為不同本：
- 書名不同的另一本書，即使作者相同（例如同一作者的另一部著作）
- 關於這本書的研究、評論、導讀、會議論文、展演或活動記錄
- 只是收錄它的叢書、全集、套書，或名稱相近但主題不同的書

只輸出 JSON：{"same_book": true 或 false, "confidence": 0 到 1（你對這個判斷有多確定）, "reason": "一句話理由"}"""


def _llm_user(r: dict) -> str:
    return (
        f"原始書目：書名＝{r['orig_title']}；作者＝{r['orig_authors'] or '（無）'}\n"
        f"配到的記錄：書名＝{r['matched_title']}；作者＝{r['matched_authors'] or '（無）'}"
    )


def run_llm(todo: list[dict], workers: int) -> None:
    from openai import OpenAI

    from library_agent.config import get_settings

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    model = settings.llm_model_mini
    print(f"  llm model: {model}", flush=True)

    def judge(r: dict) -> None:
        last_err = None
        for _ in range(3):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": _LLM_SYSTEM},
                        {"role": "user", "content": _llm_user(r)},
                    ],
                )
                data = json.loads(resp.choices[0].message.content)
                # 以 same_book 為準；confidence 是「對這個判斷的信心」，換算成同一本機率
                same = bool(data.get("same_book"))
                conf = data.get("confidence")
                conf = min(max(float(conf), 0.5), 1.0) if isinstance(conf, (int, float)) else 1.0
                r["llm_same"] = same
                r["llm_p"] = round(conf if same else 1.0 - conf, 4)
                r["llm_reason"] = str(data.get("reason", ""))[:200]
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2)
        r["llm_reason"] = f"ERROR: {last_err}"

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(judge, r) for r in todo]
        for i, _ in enumerate(as_completed(futures), 1):
            if i % 25 == 0 or i == len(todo):
                print(f"  llm {i}/{len(todo)}  ({time.time() - t0:.0f}s)", flush=True)


# ── 評估 ──────────────────────────────────────────────────────────────

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate(rows: list[dict], method: str, threshold: float) -> dict | None:
    """正類 = 不同本（要被抓去人工審核的配錯）。"""
    scored = [(r, _f(r.get(f"{method}_p"))) for r in rows]
    scored = [(r, p) for r, p in scored if p is not None]
    if not scored:
        return None

    tp = fp = tn = fn = 0
    per_stratum = {"same": [0, 0], "diff": [0, 0]}  # [correct, total]
    for r, p in scored:
        flagged = p < threshold          # 判「不同本」→ 送人工審核
        mismatch = not r["gold_same"]
        if flagged and mismatch:
            tp += 1
        elif flagged and not mismatch:
            fp += 1
        elif not flagged and mismatch:
            fn += 1
        else:
            tn += 1
        s = r["stratum"]
        if s in per_stratum:
            per_stratum[s][1] += 1
            per_stratum[s][0] += int(flagged == mismatch)

    n = len(scored)
    acc_s = {s: (c / t if t else None) for s, (c, t) in per_stratum.items()}
    weighted = (sum(_W[s] * acc_s[s] for s in _W)
                if all(acc_s[s] is not None for s in _W) else None)
    return {
        "n": n,
        "acc": (tp + tn) / n,
        "acc_weighted": weighted,
        "acc_same": acc_s["same"],
        "acc_diff": acc_s["diff"],
        "catch": tp / (tp + fn) if tp + fn else None,          # 配錯被抓到的比例
        "false_flag": fp / (fp + tn) if fp + tn else None,     # 配對正確卻被誤送審的比例
        "precision": tp / (tp + fp) if tp + fp else None,      # 送審的裡面真的是配錯的比例
    }


def _pct(v):
    return "   -  " if v is None else f"{v * 100:5.1f}%"


def report(rows: list[dict], methods: list[str], threshold: float) -> str:
    lines = [f"n = {len(rows)}   threshold = {threshold}（機率 < 門檻 → 判不同本、送人工審核）",
             f"正類 = 不同本（配錯）。人工標註：同一本 {sum(r['gold_same'] for r in rows)}、"
             f"不同本 {sum(not r['gold_same'] for r in rows)}",
             "",
             f"{'method':6} {'n':>4} {'acc':>7} {'acc加權':>7} {'similar':>7} {'diverge':>7} "
             f"{'抓到配錯':>7} {'誤送審':>7} {'送審精確':>7}"]
    for m in methods:
        e = evaluate(rows, m, threshold)
        if e is None:
            lines.append(f"{m:6}  (no results)")
            continue
        lines.append(f"{m:6} {e['n']:>4} {_pct(e['acc'])} {_pct(e['acc_weighted'])} "
                     f"{_pct(e['acc_same'])} {_pct(e['acc_diff'])} {_pct(e['catch'])} "
                     f"{_pct(e['false_flag'])} {_pct(e['precision'])}")

    lines += ["", "門檻掃描（acc 加權 / 抓到配錯 / 誤送審）："]
    for m in methods:
        if evaluate(rows, m, threshold) is None:
            continue
        cells = []
        for t in (0.3, 0.5, 0.7, 0.9):
            e = evaluate(rows, m, t)
            cells.append(f"t={t}: {_pct(e['acc_weighted'])}/{_pct(e['catch'])}/{_pct(e['false_flag'])}")
        lines.append(f"  {m:5} " + "  ".join(cells))

    lines += ["",
              "欄位說明：acc=原始抽樣準確率；acc加權=依母體 same/diff 比例還原；",
              "similar/diverge=兩層各自準確率；抓到配錯=配錯被送審比例(越高越好)；",
              "誤送審=正確配對被誤送審比例(越低越好)；送審精確=送審裡真的配錯的比例。"]
    return "\n".join(lines)


# ── main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 筆（試跑用）")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--workers", type=int, default=4, help="LLM 並行數")
    ap.add_argument("--rerun", nargs="?", const="all", default="",
                    help="忽略快取重算；不帶值=全部，或指定方法如 --rerun llm")
    args = ap.parse_args()

    methods = [m.strip() for m in args.methods.split(",") if m.strip() in METHODS]
    rows = load_rows()
    if args.limit:
        rows = rows[: args.limit]
    print(f"載入 {len(rows)} 筆人工標註；方法：{methods}")

    rerun = set(METHODS) if args.rerun == "all" else {m for m in args.rerun.split(",") if m}
    cache = load_cache()
    keep_cols = {"rule": ("rule_p",), "laya": ("laya_p",),
                 "llm": ("llm_same", "llm_p", "llm_reason")}
    for r in rows:
        c = cache.get(r["citation_id"], {})
        for m, cols in keep_cols.items():
            if m in rerun:
                continue
            for k in cols:
                if c.get(k, "") != "":
                    r[k] = c[k]
    # 快取裡不在本次 rows 的筆數也保留，避免 --limit 試跑把結果檔洗掉
    kept = [c for cid, c in cache.items() if cid not in {r["citation_id"] for r in rows}]
    for c in kept:
        c["gold_same"] = c.get("gold_same") in (True, "True", "true", "1")

    if "rule" in methods:
        for r in rows:
            r["rule_p"] = round(rule_score(r), 4)
        print(f"  rule done（opencc 繁簡折疊：{'有' if _t2s else '沒裝，略過'}）")

    if "laya" in methods:
        todo = [r for r in rows if _f(r.get("laya_p")) is None]
        print(f"laya：{len(todo)} 筆待算")
        if todo:
            run_laya(todo)
            save_results(rows + kept)

    if "llm" in methods:
        todo = [r for r in rows if _f(r.get("llm_p")) is None]
        print(f"llm：{len(todo)} 筆待算")
        if todo:
            run_llm(todo, args.workers)
            save_results(rows + kept)   # 先落地，避免後續出錯讓 API 結果白跑

    out = save_results(rows + kept)
    text = report(rows, methods, args.threshold)
    print("\n" + text)
    try:
        _SUMMARY.write_text(text, encoding="utf-8")
        summary_path = _SUMMARY
    except PermissionError:
        summary_path = _SUMMARY.with_name(f"{_SUMMARY.stem}_{int(time.time())}.txt")
        summary_path.write_text(text, encoding="utf-8")
    print(f"\n逐筆結果 → {out}\n摘要 → {summary_path}")


if __name__ == "__main__":
    main()
