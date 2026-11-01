"""審核決定的資料庫欄位與 buy_* 屬性（用獨立測試資料庫，建表由 models 產生）。

採購書名不覆蓋驗證器配到的 canonical_*；下游讀 buy_title / buy_isbn / buy_authors：
有採購書名就用採購欄位，否則用 canonical_*。ISBN 不能 coalesce——「買原書」沒有 ISBN 時
不能沿用配到那一本的 ISBN（那正是配錯的來源）。
"""
from sqlalchemy import select

from library_agent.db.models import Citation, Course, VerifiedBook


def seed(S, **vb_kw):
    with S() as s:
        s.add(Course(course_id="R1", course_name="課-R1", semester="114-1", source_file="t",
                     raw_content="t", enrolled_count=30))
        s.flush()
        c = Citation(course_id="R1", title="原書名", authors="[]", is_required=True,
                     raw_mention="指定", confidence=0.9)
        s.add(c)
        s.flush()
        kw = dict(citation_id=c.id, canonical_title="配到的書", canonical_authors='["別人"]',
                  isbn_13="9780306406157", source="nla", verified=True)
        kw.update(vb_kw)
        vb = VerifiedBook(**kw)
        s.add(vb)
        s.commit()
        return vb.id


def test_new_columns_exist_and_default_to_null(isolated_db):
    vb_id = seed(isolated_db)

    with isolated_db() as s:
        vb = s.get(VerifiedBook, vb_id)

        assert vb.purchase_title is None and vb.purchase_authors is None and vb.purchase_isbn is None
        assert vb.review_choice is None and vb.reviewed_at is None and vb.review_applied_at is None


def test_buy_properties_fall_back_to_canonical_when_there_is_no_purchase_title(isolated_db):
    vb_id = seed(isolated_db)

    with isolated_db() as s:
        vb = s.get(VerifiedBook, vb_id)

        assert vb.buy_title == "配到的書"
        assert vb.buy_authors == '["別人"]'
        assert vb.buy_isbn == "9780306406157"


def test_buy_properties_use_the_purchase_fields_when_a_purchase_title_is_set(isolated_db):
    vb_id = seed(isolated_db, purchase_title="原書名", purchase_authors='["原作者"]', purchase_isbn="9780199296095")

    with isolated_db() as s:
        vb = s.get(VerifiedBook, vb_id)

        assert (vb.buy_title, vb.buy_authors, vb.buy_isbn) == ("原書名", '["原作者"]', "9780199296095")


def test_buy_isbn_does_not_fall_back_to_the_matched_isbn_when_a_purchase_title_is_set(isolated_db):
    # 買原書、原書沒有有效 ISBN：不能用配到那本的 ISBN
    vb_id = seed(isolated_db, purchase_title="原書名", purchase_authors="[]", purchase_isbn=None)

    with isolated_db() as s:
        vb = s.get(VerifiedBook, vb_id)

        assert vb.isbn_13 == "9780306406157" and vb.buy_isbn is None


def test_buy_expressions_work_in_sql_and_agree_with_python(isolated_db):
    plain = seed(isolated_db)
    with isolated_db() as s:
        s.add(Course(course_id="R2", course_name="課-R2", semester="114-1", source_file="t", raw_content="t"))
        s.flush()
        c = Citation(course_id="R2", title="另一本", authors="[]", is_required=True, raw_mention="指定", confidence=0.9)
        s.add(c)
        s.flush()
        s.add(VerifiedBook(citation_id=c.id, canonical_title="配到的別本", canonical_authors="[]",
                           isbn_13="9780306406157", source="nla", verified=True,
                           purchase_title="我要買的", purchase_authors="[]", purchase_isbn=None))
        s.commit()

    with isolated_db() as s:
        rows = s.execute(
            select(VerifiedBook.id, VerifiedBook.buy_title, VerifiedBook.buy_isbn, VerifiedBook.buy_authors)
            .order_by(VerifiedBook.id)
        ).all()
        objs = s.scalars(select(VerifiedBook).order_by(VerifiedBook.id)).all()

        assert [(r[1], r[2], r[3]) for r in rows] == [(o.buy_title, o.buy_isbn, o.buy_authors) for o in objs]
        assert rows[0][0] == plain and rows[0][1] == "配到的書" and rows[1][1] == "我要買的" and rows[1][2] is None
