"""回饋迴路對照實驗：同一批需 AI 推薦的課，比較「無迴路」vs「有迴路」的驗證成功率。

無迴路 = discoverer→validator 跑一次（discovery_iteration 停在 0，路由不回 discoverer）。
有迴路 = 完整 pipeline（validator→discoverer 條件邊，迭代上限 _MAX_DISCOVERY_ITERATIONS，
即最多重推 _MAX_DISCOVERY_ITERATIONS − 1 次；預設上限 2 ⇒ 最多重推 1 次）。

用法：
    python -u experiments/feedback_loop_experiment.py --sample 25
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select, func

from library_agent.db.models import Citation, Course, VerifiedBook as VerifiedBookDB
from library_agent.db.session import SessionLocal


def _ai_verify_stats(course_ids):
    """回傳 (AI推薦書總數, 驗證成功數) — 只算未 superseded 的最終書目。"""
    with SessionLocal() as s:
        rows = s.execute(
            select(VerifiedBookDB.verified)
            .join(Citation, Citation.id == VerifiedBookDB.citation_id)
            .where(
                Citation.course_id.in_(course_ids),
                Citation.superseded_by_iteration.is_(None),
                Citation.raw_mention.like("[AI推薦%"),
            )
        ).all()
    total = len(rows)
    success = sum(1 for (v,) in rows if v)
    return total, success


def _sample_recommend_courses(n):
    """抽 n 門『Parser 沒抓到書』的課（會觸發 AI 推薦）。"""
    with SessionLocal() as s:
        courses = s.scalars(
            select(Course)
            .outerjoin(Citation, Citation.course_id == Course.course_id)
            .where(Citation.id == None)  # noqa: E711
            .limit(n)
        ).all()
        return [c.course_id for c in courses]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=25)
    args = ap.parse_args()

    course_ids = _sample_recommend_courses(args.sample)
    print(f"抽樣 {len(course_ids)} 門需 AI 推薦的課：{course_ids}", flush=True)

    # 為公平：兩組都從乾淨狀態跑。實作上先跑「無迴路」記錄，再清掉本批 AI citation 重跑「有迴路」。
    # 這裡提供骨架；實際跑法在 Step 2 說明（需先確認清理範圍安全）。
    print("（實驗執行步驟見 plan Task 6 Step 2）", flush=True)


if __name__ == "__main__":
    main()
