"""Paired evaluation 分層抽樣：從所有 verified 配對成功的書中抽樣，
供人工標「生成對不對」×「配對對不對」，用於審稿人要求的 transition-count 表。

分層依據：原始書名 vs 配到的 canonical 書名是否重疊
  - same 層（重疊，多半配對正確）：抽 40
  - diff 層（差異大，配錯集中於此）：抽 80
兩層各記母體大小，供後續加權還原到整體配對錯誤率。

輸出 paired_eval_to_annotate.csv，欄位對齊人工標註：
  兩個待標欄「標註_生成正確(對/錯)」「標註_配對正確(對/錯)」

用法：python experiments/paired_eval_sample.py
"""
import csv
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select

from library_agent.db.models import Citation, VerifiedBook
from library_agent.db.session import SessionLocal

_SEED = 42
_N_SAME = 40
_N_DIFF = 80
_OUT = Path(__file__).resolve().parent / "paired_eval_to_annotate.csv"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", (s or "").lower())


def _is_same(orig: str, canon: str) -> bool:
    """原始書名核心（去副標）是否與 canonical 重疊——判分層用，非配對正確與否。"""
    o, c = _norm(orig), _norm(canon)
    if not o or not c:
        return False
    o_core = _norm(re.sub(r"[:：,，].*", "", orig))
    return bool(o_core and (o_core in c or c in o or o == c))


def main():
    with SessionLocal() as s:
        rows = s.execute(
            select(
                Citation.id, Citation.course_id, Citation.title, Citation.authors,
                VerifiedBook.canonical_title, VerifiedBook.canonical_authors,
                VerifiedBook.isbn_13, VerifiedBook.source,
            )
            .join(VerifiedBook, VerifiedBook.citation_id == Citation.id)
            .where(VerifiedBook.verified == True, VerifiedBook.canonical_title.isnot(None))  # noqa: E712
        ).all()

    same, diff = [], []
    for r in rows:
        (same if _is_same(r.title, r.canonical_title) else diff).append(r)

    print(f"母體：same 層 {len(same)} 本、diff 層 {len(diff)} 本、合計 {len(rows)}")

    random.seed(_SEED)
    pick_same = random.sample(same, min(_N_SAME, len(same)))
    pick_diff = random.sample(diff, min(_N_DIFF, len(diff)))
    picked = [("same", r) for r in pick_same] + [("diff", r) for r in pick_diff]

    def _authors(a):
        try:
            return " / ".join(json.loads(a)) if a else ""
        except Exception:
            return a or ""

    with open(_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "序號", "citation_id", "課程ID", "分層",
            "原始書名(生成)", "原始作者",
            "配到的書名(canonical)", "配到的作者", "配到ISBN", "來源",
            "標註_生成正確(對/錯)", "標註_配對正確(對/錯)", "標註_備註",
        ])
        for i, (stratum, r) in enumerate(picked, 1):
            w.writerow([
                i, r.id, r.course_id, stratum,
                r.title, _authors(r.authors),
                r.canonical_title, _authors(r.canonical_authors),
                r.isbn_13 or "", r.source,
                "", "", "",
            ])

    print(f"抽樣 {len(picked)} 本（same {len(pick_same)} + diff {len(pick_diff)}）→ {_OUT}")
    print(f"母體比例：same {len(same)/len(rows)*100:.1f}% / diff {len(diff)/len(rows)*100:.1f}%（後續加權用）")


if __name__ == "__main__":
    main()
