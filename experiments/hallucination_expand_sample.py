"""擴大 AI 推薦幻覺率評估：從 619 本 AI 推薦書抽樣，供人工判斷『原始推薦書名是否真書』。

回應審稿人 #2（AI 推薦幻覺樣本只 40 本、太小）。
評『原始推薦書名』是否真實存在（不靠驗證結果，避免『verified=true 即真書』的循環論證）。

抽樣：verified=true 的 611 本隨機抽 50 + verified=false 的 8 本全取 = 58 本。
      失敗那 8 本是最可能幻覺的（查無），全標。

輸出 hallucination_expand_to_annotate.csv，欄位：
  原始推薦書名、作者、推薦理由、驗證狀態、[待標]是否真書(是/否=幻覺)

用法：python experiments/hallucination_expand_sample.py
"""
import csv
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select

from library_agent.db.models import Citation, VerifiedBook
from library_agent.db.session import SessionLocal

_SEED = 42
_N_VERIFIED = 50
_OUT = Path(__file__).resolve().parent / "hallucination_expand_to_annotate.csv"


def main():
    with SessionLocal() as s:
        rows = s.execute(
            select(Citation.id, Citation.title, Citation.authors,
                   Citation.raw_mention, VerifiedBook.verified)
            .join(VerifiedBook, VerifiedBook.citation_id == Citation.id)
            .where(Citation.raw_mention.like("%AI推薦%"))
        ).all()

    verified = [r for r in rows if r.verified]
    failed = [r for r in rows if not r.verified]
    print(f"AI 推薦：verified={len(verified)}、failed={len(failed)}")

    random.seed(_SEED)
    pick = random.sample(verified, min(_N_VERIFIED, len(verified))) + failed

    import json

    def _auth(a):
        try:
            return " / ".join(json.loads(a)) if a else ""
        except Exception:
            return a or ""

    def _reason(rm):
        # raw_mention 格式：[AI推薦] 理由...
        return (rm or "").replace("[AI推薦]", "").strip()[:50]

    with open(_OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["序號", "citation_id", "原始推薦書名", "作者", "推薦理由",
                    "驗證狀態", "標註_是否真書(是/否，否=幻覺)"])
        for i, r in enumerate(pick, 1):
            w.writerow([i, r.id, r.title, _auth(r.authors), _reason(r.raw_mention),
                        "verified" if r.verified else "failed", ""])

    print(f"抽樣 {len(pick)} 本（verified {len(pick)-len(failed)} + failed {len(failed)}）→ {_OUT}")


if __name__ == "__main__":
    main()
