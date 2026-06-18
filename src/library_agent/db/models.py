from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass

# 對應 RawSyllabus，存 Crawler 讀進來的每門課
class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    course_name: Mapped[str] = mapped_column(String(200), nullable=False)
    instructor: Mapped[str | None] = mapped_column(String(100))
    enrolled_count: Mapped[int] = mapped_column(Integer, default=0)
    semester: Mapped[str] = mapped_column(String(10), nullable=False)
    source_file: Mapped[str] = mapped_column(String(200), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    citations: Mapped[list["Citation"]] = relationship(back_populates="course")

# 對應 BookCitation，存 Parser 解析出的每筆書目
class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.course_id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    authors: Mapped[str | None] = mapped_column(Text)        # JSON array string
    edition: Mapped[str | None] = mapped_column(String(50))
    isbn: Mapped[str | None] = mapped_column(String(20))
    publisher: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_mention: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    course: Mapped["Course"] = relationship(back_populates="citations")
    verified_book: Mapped["VerifiedBook | None"] = relationship(back_populates="citation")

# Validator 驗證完的書目
class VerifiedBook(Base):
    __tablename__ = "verified_books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    citation_id: Mapped[int] = mapped_column(ForeignKey("citations.id"), nullable=False)
    canonical_title: Mapped[str] = mapped_column(String(500), nullable=False)
    canonical_authors: Mapped[str | None] = mapped_column(Text)   # JSON array string
    isbn_13: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(50))               # google_books | nla
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)

    citation: Mapped["Citation"] = relationship(back_populates="verified_book")
    holding: Mapped["HoldingCheck | None"] = relationship(back_populates="verified_book")

# Librarian 查 Alma 的結果
class HoldingCheck(Base):
    __tablename__ = "holding_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    verified_book_id: Mapped[int] = mapped_column(ForeignKey("verified_books.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)   # HoldingStatus enum value
    holdings_count: Mapped[int] = mapped_column(Integer, default=0)
    alma_mms_id: Mapped[str | None] = mapped_column(String(100))
    matched_edition: Mapped[str | None] = mapped_column(String(50))

    verified_book: Mapped["VerifiedBook"] = relationship(back_populates="holding")
    recommendation: Mapped["Recommendation | None"] = relationship(back_populates="holding")

# 最終採購建議
class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.course_id"), nullable=False)
    holding_id: Mapped[int] = mapped_column(ForeignKey("holding_checks.id"), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)  # PurchasePriority enum value
    suggested_copies: Mapped[int] = mapped_column(Integer, default=1)
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    holding: Mapped["HoldingCheck"] = relationship(back_populates="recommendation")
