"""採購「需求冊數」的唯一一份算式。

recommender（_decide）、追溯畫面（_calc）、儀表板（「需求 N → 建議 M」）都用這裡，避免三處各抄一份而分家。
刻意放在輕量的獨立模組：recommender 在 import 時會建構 OpenAI client，儀表板不該為了算需求而載入它。

  need     = ceil(總人數 / 每本服務人數)；總人數 0（人數不明）時為 1
  uncapped = max(need − 現有實體冊數, 0)，**不套單本上限**；已有電子書時為 0
recommender 的建議冊數 = min(uncapped, 單本上限)（tests/test_demand.py 把兩者鎖在一起）。
"""
import math

from library_agent.config import get_settings
from library_agent.state import HoldingStatus

_settings = get_settings()


def total_enrolled(course_enrolments) -> int:
    """(course_id, 人數) 的可迭代物件 → 總人數。同一門課重複列只算一次（與 recommender.merge_group 一致）。"""
    per_course: dict = {}
    for course_id, enrolled in course_enrolments:
        per_course.setdefault(course_id, enrolled or 0)
    return sum(per_course.values())


def status_of(raw: str | None) -> HoldingStatus:
    """資料庫存的館藏狀態字串 → enum；不認得的當 unknown（不會是電子書），讀取端不該因此崩潰。"""
    try:
        return HoldingStatus(raw)
    except ValueError:
        return HoldingStatus.UNKNOWN


def need_copies(enrolled_total: int) -> int:
    per = max(_settings.students_per_copy, 1)
    return math.ceil(enrolled_total / per) if enrolled_total > 0 else 1


def uncapped_copies(status: HoldingStatus, enrolled_total: int, current_copies: int) -> int:
    if status == HoldingStatus.OWNED_EBOOK:
        return 0
    return max(need_copies(enrolled_total) - current_copies, 0)
