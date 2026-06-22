import argparse
import csv
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from library_agent.db.models import (
    Citation,
    Course,
    HoldingCheck,
    Recommendation,
    VerifiedBook,
)
from library_agent.db.session import SessionLocal
from library_agent.state import PurchasePriority

_PRIORITY_ORDER = [
    PurchasePriority.HIGH,
    PurchasePriority.MEDIUM,
    PurchasePriority.LOW,
    PurchasePriority.SKIP,
]


def _fetch_report_rows() -> list[dict]:
    with SessionLocal() as session:
        rows = session.execute(
            select(Recommendation, Course, VerifiedBook, Citation)
            .join(Course, Course.course_id == Recommendation.course_id)
            .join(HoldingCheck, HoldingCheck.id == Recommendation.holding_id)
            .join(VerifiedBook, VerifiedBook.id == HoldingCheck.verified_book_id)
            .join(Citation, Citation.id == VerifiedBook.citation_id)
            .order_by(Recommendation.priority, Course.course_name)
        ).all()

    return [
        {
            "priority": rec.priority,
            "course_id": course.course_id,
            "course_name": course.course_name,
            "title": vb.canonical_title,
            "is_required": citation.is_required,
            "suggested_copies": rec.suggested_copies,
            "rationale": rec.rationale or "",
        }
        for rec, course, vb, citation in rows
    ]


def _print_report(rows: list[dict]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  圖書館採購建議報告  （{now}）")
    print(f"{'='*60}")

    by_priority: dict[str, list[dict]] = {p.value: [] for p in _PRIORITY_ORDER}
    for row in rows:
        by_priority.setdefault(row["priority"], []).append(row)

    for priority in _PRIORITY_ORDER:
        group = by_priority[priority.value]
        if not group:
            continue
        label = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "🟢 LOW", "skip": "⚪ SKIP"}.get(
            priority.value, priority.value.upper()
        )
        print(f"\n{label}  （{len(group)} 筆）")
        print(f"  {'課程':<12} {'書名':<40} {'冊數':>4}  理由")
        print(f"  {'-'*12} {'-'*40} {'-'*4}  {'-'*30}")
        for r in group:
            book_type = "指" if r["is_required"] else "選"
            print(
                f"  [{book_type}] {r['course_name'][:10]:<10} "
                f"{r['title'][:38]:<38} "
                f"{r['suggested_copies']:>4}  "
                f"{r['rationale']}"
            )

    print(f"\n{'─'*60}")
    total_purchase = sum(r["suggested_copies"] for r in rows if r["priority"] != "skip")
    counts = {p.value: sum(1 for r in rows if r["priority"] == p.value) for p in _PRIORITY_ORDER}
    for p in _PRIORITY_ORDER:
        if counts[p.value]:
            print(f"  {p.value:<8} {counts[p.value]:>4} 筆")
    print(f"  {'合計建議採購冊數':<10} {total_purchase:>4} 冊")
    print(f"{'='*60}\n")


def _write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["priority", "course_id", "course_name", "title",
                        "is_required", "suggested_copies", "rationale"],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"報表已存至 {path}")


def run(limit: int | None = None) -> None:
    from library_agent.graph import pipeline
    print("啟動 pipeline...")
    initial_state = {"limit": limit} if limit else {}
    result = pipeline.invoke(initial_state)
    errors = result.get("errors", [])
    if errors:
        print(f"\n共 {len(errors)} 個錯誤：")
        for e in errors:
            print(f"  {e}")


def report(output: str | None = None) -> None:
    rows = _fetch_report_rows()
    if not rows:
        print("資料庫中尚無採購建議，請先執行 pipeline。")
        return
    _print_report(rows)
    if output:
        _write_csv(rows, output)


def cli() -> None:
    parser = argparse.ArgumentParser(description="圖書館採購決策支援系統")
    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", help="執行完整 pipeline")
    run_cmd.add_argument("--limit", type=int, default=None, help="測試用：限制課程筆數")

    report_cmd = sub.add_parser("report", help="輸出採購建議報表")
    report_cmd.add_argument("--output", "-o", default=None, help="儲存為 CSV 檔案路徑")

    args = parser.parse_args()

    if args.command == "run":
        run(limit=args.limit)
        report()
    elif args.command == "report":
        report(output=args.output)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    cli()
