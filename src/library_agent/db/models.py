from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    department: Mapped[str | None] = mapped_column(String(100))  # 開課系級（xlsx「開課系級」欄）
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
    # AI 推薦書驗證失敗被回饋迴路換掉時，標記在第幾輪被取代（None=仍有效）。保留舊列供實驗追溯。
    superseded_by_iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    # 人工審核決定：None（不需/未產生）/ pending / approved / rejected
    review_status: Mapped[str | None] = mapped_column(String(20))

    # match_auditor 的配對稽核：確認「配到的權威記錄」是否真的是原書
    # None = 尚未稽核（也是重跑時的待辦標記）；same = 同一本；mismatch = 配錯
    pairing_verdict: Mapped[str | None] = mapped_column(String(10))
    pairing_judge: Mapped[str | None] = mapped_column(String(10))   # rule | llm
    pairing_reason: Mapped[str | None] = mapped_column(Text)

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

# 最終採購建議：一本書一筆（同一本書被多門課用到時合併，見 book_identity 與 recommender）。
# course_id / holding_id 是「代表課程 / 代表館藏」（該組館藏冊數最大者）；
# 這本書被哪些課用到記在 recommendation_courses。
class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (Index("uq_recommendations_book_key", "book_key", unique=True),)

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
    # 這本書的識別鍵（isbn:<13碼> 或 title:<正規化書名>[#n]）。舊格式（一課一書一筆）的列為 NULL，
    # recommender 下次執行時會清掉並重建；唯一索引允許多個 NULL。
    book_key: Mapped[str | None] = mapped_column(String(500))

    holding: Mapped["HoldingCheck"] = relationship(back_populates="recommendation")
    courses: Mapped[list["RecommendationCourse"]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", passive_deletes=True,
    )


# 這本書被哪些課用到：一個書目一列。人數是計算當時用的值（快照），is_required 是該課自己的類別。
# 外鍵都是 ON DELETE CASCADE：validator / librarian 會直接 DELETE recommendations、
# parser 會先刪舊 citations，沒有 cascade 這些節點會被外鍵擋住而崩潰。
class RecommendationCourse(Base):
    __tablename__ = "recommendation_courses"
    __table_args__ = (
        UniqueConstraint("recommendation_id", "citation_id", name="uq_reccourse_rec_citation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), nullable=False)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.course_id"), nullable=False)
    citation_id: Mapped[int] = mapped_column(
        ForeignKey("citations.id", ondelete="CASCADE"), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enrolled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    recommendation: Mapped["Recommendation"] = relationship(back_populates="courses")

# Pipeline 執行時每個節點的即時狀態（供 dashboard 流程圖顯示進度）
class PipelineNodeRun(Base):
    __tablename__ = "pipeline_node_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(50), nullable=False)
    node_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pending | running | done | error
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
