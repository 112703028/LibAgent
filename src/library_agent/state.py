from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field


class HoldingStatus(str, Enum):
    OWNED_PHYSICAL = "owned_physical"
    OWNED_EBOOK = "owned_ebook"
    MISSING = "missing"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class PurchasePriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SKIP = "skip"


class RawSyllabus(BaseModel):
    course_id: str
    course_name: str
    department: str | None = None  # 開課系級（xlsx「開課系級 Department and Level」欄）
    instructor: str | None = None
    enrolled_count: int = 0
    semester: str
    source_file: str
    raw_content: str
    content_type: str = "xlsx"
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BookCitation(BaseModel):
    course_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    edition: str | None = None
    isbn: str | None = None
    publisher: str | None = None
    year: int | None = None
    is_required: bool = True
    raw_mention: str = ""
    confidence: float = 0.0


class VerifiedBook(BaseModel):
    citation: BookCitation
    canonical_title: str
    canonical_authors: list[str] = Field(default_factory=list)
    isbn_13: str | None = None
    source: str  # google_books | nla
    verified: bool = False
    requires_human_review: bool = False


class HoldingCheck(BaseModel):
    book: VerifiedBook
    status: HoldingStatus = HoldingStatus.UNKNOWN
    holdings_count: int = 0
    alma_mms_id: str | None = None
    matched_edition: str | None = None


def _merge(left: list, right: list) -> list:
    return left + right


class AgentState(TypedDict, total=False):
    # 領域資料一律存 DB（single source of truth）；
    # state 只保留控制流（limit / course_ids）與跨節點彙整的 errors。
    errors: Annotated[list[str], _merge]
    limit: int | None  # 測試用：限制課程筆數
    course_ids: list[str] | None  # crawler 傳給下游的課程 ID 清單（limit 時使用）
    source_files: list[str] | None  # 這次只讀 data/ 裡這些 xlsx 檔名（None/空＝全部）
    departments: list[str] | None  # 這次只處理這些開課系級的課（None/空＝全部）
