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


def _pick_unprocessed(syllabi: list[RawSyllabus], parsed_ids: set[str], limit: int) -> list[str]:
    """挑前 limit 筆「還沒處理過的新課」的 course_id。
    已有 Citation（parser 跑過）或佔位符（parser 會跳過、永遠不會有 Citation）都算已處理，
    這樣連按 limit=N 會依序推進到新課，不會每次都選到同幾門。"""
    from library_agent.agents.parser import _needs_parsing
    return [s.course_id for s in syllabi if _needs_parsing(s, parsed_ids)][:limit]


def _select_xlsx_paths(all_paths: list[Path], source_files: list[str] | None) -> list[Path]:
    """決定這次 crawler 讀哪些 xlsx。source_files 為 None 或空 → 讀全部（跟原行為一致）；
    否則只讀檔名（.name）在 source_files 裡的，忽略清單中不存在的檔名。"""
    if not source_files:
        return all_paths
    wanted = set(source_files)
    return [p for p in all_paths if p.name in wanted]


def crawler_node(state: AgentState) -> AgentState:
    # 只讀被選中的 xlsx（未指定＝全部）；選中的課程照舊全寫進 courses 表。
    all_paths = sorted(DATA_DIR.glob("*.xlsx"))
    paths = _select_xlsx_paths(all_paths, state.get("source_files"))
    syllabi = _dedup([s for path in paths for s in _load_xlsx(path)])
    _save_to_db(syllabi)

    limit = state.get("limit")
    if not limit:
        return {"course_ids": None}

    from library_agent.db.models import Citation
    with SessionLocal() as session:
        parsed_ids = set(session.scalars(select(Citation.course_id).distinct()).all())

    return {"course_ids": _pick_unprocessed(syllabi, parsed_ids, limit)}


if __name__ == "__main__":
    crawler_node({})
    with SessionLocal() as session:
        n = len(session.scalars(select(Course.course_id)).all())
    print(f"DB 共 {n} 門課程")
