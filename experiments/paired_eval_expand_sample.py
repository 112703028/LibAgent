"""擴大 paired evaluation 樣本：在已標的 120 本之外，再分層抽 180 本（總 300）。

排除已標的 120 本（讀 paired_eval_to_annotate.csv 的 citation_id）。
分層同原設計（same 書名重疊 / diff 書名差異），維持 same:diff = 1:2 → 新抽 same 60 + diff 120。
預填配對建議於備註欄（繁簡/異體/副標=對；配到不相關=錯），降低人工負擔。

用法：python experiments/paired_eval_expand_sample.py
輸出：paired_eval_expand_to_annotate.csv
"""
import csv
import json
import random
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select

from library_agent.db.models import Citation, VerifiedBook
from library_agent.db.session import SessionLocal

_SEED = 43  # 與原抽樣(42)不同，且排除已抽，確保不重複
_N_SAME, _N_DIFF = 60, 120
_ORIG = Path(__file__).resolve().parent / "paired_eval_to_annotate.csv"
_OUT = Path(__file__).resolve().parent / "paired_eval_expand_to_annotate.csv"

_ZH = str.maketrans("台湾国说学习书体艺术们语传统诗词汉", "臺灣國說學習書體藝術們語傳統詩詞漢")


def _norm(s):
    return re.sub(r"[^a-z0-9一-鿿]", "", (s or "").lower())


def _zhfold(s):
    return unicodedata.normalize("NFKC", _norm(s)).translate(_ZH)


def _is_same(orig, canon):
    o, c = _norm(orig), _norm(canon)
    if not o or not c:
        return False
    o_core = _norm(re.sub(r"[:：,，].*", "", orig))
    return bool(o_core and (o_core in c or c in o or o == c))


def _suggest(orig, canon):
    oc = _zhfold(re.sub(r"[:：,，=＝].*", "", orig))
    cf, of = _zhfold(canon), _zhfold(orig)
    if not oc:
        return "?"
    return "對" if (oc in cf or of in cf or cf in of) else "錯"


def main():
    # 已標的 citation_id
    done = set()
    with open(_ORIG, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            cid = next((r[k] for k in r if "citation_id" in k), "")
            if cid.strip():
                done.add(int(cid))
    print(f"已標(排除): {len(done)} 本")

    with SessionLocal() as s:
        rows = s.execute(
            select(Citation.id, Citation.course_id, Citation.title, Citation.authors,
                   VerifiedBook.canonical_title, VerifiedBook.canonical_authors,
                   VerifiedBook.isbn_13, VerifiedBook.source)
            .join(VerifiedBook, VerifiedBook.citation_id == Citation.id)
            .where(VerifiedBook.verified == True,  # noqa: E712
                   VerifiedBook.canonical_title.isnot(None))
        ).all()

    rows = [r for r in rows if r.id not in done]  # 排除已標
    same = [r for r in rows if _is_same(r.title, r.canonical_title)]
    diff = [r for r in rows if not _is_same(r.title, r.canonical_title)]
    print(f"剩餘母體：same {len(same)}、diff {len(diff)}")

    random.seed(_SEED)
    pick = ([("same", r) for r in random.sample(same, min(_N_SAME, len(same)))]
            + [("diff", r) for r in random.sample(diff, min(_N_DIFF, len(diff)))])

    def _auth(a):
        try:
            return " / ".join(json.loads(a)) if a else ""
        except Exception:
            return a or ""

    with open(_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["序號", "citation_id", "課程ID", "分層",
                    "原始書名(生成)", "原始作者",
                    "配到的書名(canonical)", "配到的作者", "配到ISBN", "來源",
                    "標註_生成正確(對/錯)", "標註_配對正確(對/錯)", "備註"])
        for i, (st, r) in enumerate(pick, 1):
            w.writerow([i, r.id, r.course_id, st,
                        r.title, _auth(r.authors),
                        r.canonical_title, _auth(r.canonical_authors),
                        r.isbn_13 or "", r.source,
                        "", "", f"建議配對={_suggest(r.title, r.canonical_title)}"])

    print(f"新抽 {len(pick)} 本（same {sum(1 for x in pick if x[0]=='same')} + "
          f"diff {sum(1 for x in pick if x[0]=='diff')}）→ {_OUT}")


if __name__ == "__main__":
    main()
