"""Laya 冒煙測試：判斷「原始書目」與「驗證器配到的記錄」是否為同一本書。

用途：在 validator 配對之後加一道稽核（不取代 nla 的配對邏輯），先用幾個
人工已標好答案的案例，看 Laya 的 Noul（是/否機率）分不分得出「配錯」與「配對」。

案例取自 paired evaluation 的人工標註：
  - DIFF：已知配錯（羽球/游泳、散曲概論/散曲叢刊、哈姆雷/關渡藝術節…）
  - SAME：已知配對（繁簡差異、副標差異、作者寫法差異）

每筆案例用兩種輸入格式各跑一次並排比較：
  - text：兩筆書目包成一段文字
  - dict：拆成 original_title / matched_title 等結構化欄位

第一次執行會從 HuggingFace 下載模型（約數百 MB），之後走本機快取。

用法：
    python experiments/laya_same_book_smoke.py
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from laya import Router

# (原書名, 原作者, 配到書名, 配到作者, 人工標註是否同一本)
CASES = [
    ("羽球指引", "樊正治", "圖解游泳指引", "H, Robertson David / 謝璨玲", False),
    ("散曲概論", "任中敏", "散曲叢刊十五種", "任中敏", False),
    ("哈姆雷", "莎士比亞", "2012關渡藝術節 Kuandu Arts Festival", "", False),
    ("文化與文創", "漢寶德", "「點字書、數位有聲書暨閱讀推廣活動」圖書光碟", "", False),
    ("臺灣新文學思潮史綱", "趙遐秋 / 呂正惠", "台灣新文學思潮史綱", "趙遐秋 / 呂正惠", True),
    ("休閒遊憩理論與實務", "", "休閒遊憩 : 理論與實務 = Leisure and recreation", "", True),
    ("Principles of Economics", "N. Gregory Mankiw", "Principles of economics",
     "Mankiw, N. Gregory", True),
]

QUESTION = {
    "same_book": {
        "type": "noul",
        "instructions": (
            "Do the ORIGINAL citation and the MATCHED catalog record refer to the same book? "
            "Ignore traditional/simplified Chinese differences, subtitles, and edition notes."
        ),
    }
}

THRESHOLD = 0.5  # p >= 門檻判「同一本」


def state_text(o_title, o_authors, m_title, m_authors):
    """兩筆書目包成一段文字。"""
    return (
        f"ORIGINAL citation: title={o_title}; authors={o_authors or '(none)'}\n"
        f"MATCHED record: title={m_title}; authors={m_authors or '(none)'}"
    )


def state_dict(o_title, o_authors, m_title, m_authors):
    """兩筆書目拆成結構化欄位。"""
    return {
        "original_title": o_title,
        "original_authors": o_authors or "(none)",
        "matched_title": m_title,
        "matched_authors": m_authors or "(none)",
    }


FORMATS = {"text": state_text, "dict": state_dict}


def main():
    router = Router()
    correct = {name: 0 for name in FORMATS}

    print(f"{'gold':5} " + " ".join(f"{name:>13}" for name in FORMATS) + " | case")
    for o_t, o_a, m_t, m_a, gold in CASES:
        cells = []
        for name, build in FORMATS.items():
            res = router.predict(build(o_t, o_a, m_t, m_a), QUESTION)
            p = res["answers"]["same_book"]["noul"]
            ok = (p >= THRESHOLD) == gold
            correct[name] += ok
            cells.append(f"{'OK' if ok else 'XX'} p={p:.3f}")
        print(f"{'SAME' if gold else 'DIFF':5} " + " ".join(f"{c:>13}" for c in cells)
              + f" | {o_t} -> {m_t[:24]}")

    print()
    for name in FORMATS:
        print(f"{name:5}: {correct[name]}/{len(CASES)} correct at threshold {THRESHOLD}")


if __name__ == "__main__":
    main()
