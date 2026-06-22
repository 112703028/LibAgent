from pathlib import Path

import pandas as pd

from sqlalchemy import select

from library_agent.db.models import Course
from library_agent.db.session import SessionLocal
from library_agent.state import AgentState, RawSyllabus

DATA_DIR = Path(__file__).parents[3] / "data"

_COURSE_ID_COL = "科目代號\nCourse #"
_COURSE_NAME_COL = "科目名稱"
_YEAR_COL = "學年"
_SEMESTER_COL = "學期"
_BIBLIOGRAPHY_COL = "指定/參考書目\nNote"
_INSTRUCTOR_COL = "授課教師\nInstructor "


def _clean_text(text: str) -> str:
    return text.replace("_x000D_", "").replace("\r", "").strip()


def _load_xlsx(path: Path) -> list[RawSyllabus]:
    df = pd.read_excel(path, header=1)
    syllabi = []
    for _, row in df.iterrows():
        raw_content = row.get(_BIBLIOGRAPHY_COL)
        if pd.isna(raw_content) or not str(raw_content).strip():
            continue
        syllabi.append(
            RawSyllabus(
                course_id=str(row[_COURSE_ID_COL]).strip(),
                course_name=str(row[_COURSE_NAME_COL]).strip(),
                semester=f"{int(row[_YEAR_COL])}-{int(row[_SEMESTER_COL])}",
                source_file=path.name,
                instructor=str(row.get(_INSTRUCTOR_COL, "")).strip() or None,
                raw_content=_clean_text(str(raw_content)),
            )
        )
    return syllabi


def _dedup(syllabi: list[RawSyllabus]) -> list[RawSyllabus]:
    seen: dict[str, RawSyllabus] = {}
    for s in syllabi:
        seen[s.course_id] = s
    return list(seen.values())


def _save_to_db(syllabi: list[RawSyllabus]) -> None:
    with SessionLocal() as session:
        for s in syllabi:
            existing = session.scalars(
                select(Course).where(Course.course_id == s.course_id)
            ).first()
            if existing is None:
                # 只是放進 session 的暫存區，還沒進 DB，等 session.commit() 的時候才會真正寫入 DB
                session.add(Course(     
                    course_id=s.course_id,
                    course_name=s.course_name,
                    instructor=s.instructor,
                    enrolled_count=s.enrolled_count,
                    semester=s.semester,
                    source_file=s.source_file,
                    raw_content=s.raw_content,
                    fetched_at=s.fetched_at,
                ))
            else:
                changed = False
                if existing.raw_content != s.raw_content:
                    existing.raw_content = s.raw_content
                    changed = True
                if existing.instructor != s.instructor:
                    existing.instructor = s.instructor
                    changed = True
                if changed:
                    existing.fetched_at = s.fetched_at
        session.commit()


def crawler_node(state: AgentState) -> AgentState:
    syllabi = _dedup([s for path in sorted(DATA_DIR.glob("*.xlsx")) for s in _load_xlsx(path)])
    limit = state.get("limit")
    if limit:
        syllabi = syllabi[:limit]
    _save_to_db(syllabi)
    course_ids = [s.course_id for s in syllabi] if limit else None
    return {"syllabi": syllabi, "course_ids": course_ids}


if __name__ == "__main__":
    result = crawler_node({})
    print(f"共載入 {len(result['syllabi'])} 門課程")
