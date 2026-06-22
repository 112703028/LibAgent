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


class PurchaseRecommendation(BaseModel):
    course_id: str
    course_name: str
    book: VerifiedBook
    holding: HoldingCheck
    priority: PurchasePriority
    suggested_copies: int = 1
    rationale: str = ""


def _merge(left: list, right: list) -> list:
    return left + right


class AgentState(TypedDict, total=False):
    syllabi: Annotated[list[RawSyllabus], _merge]
    citations: Annotated[list[BookCitation], _merge]
    verified_books: Annotated[list[VerifiedBook], _merge]
    holdings: Annotated[list[HoldingCheck], _merge]
    recommendations: Annotated[list[PurchaseRecommendation], _merge]
    human_review_queue: Annotated[list[BookCitation], _merge]
    errors: Annotated[list[str], _merge]
    limit: int | None  # 測試用：限制課程筆數
    course_ids: list[str] | None  # crawler 傳給下游的課程 ID 清單（limit 時使用）
