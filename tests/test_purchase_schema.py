"""purchase_decisions：人對採購冊數的決定。以 book_key（同一本書的識別）為鍵，獨立於 recommendations——
recommendations 是衍生資料，librarian／recommender 會刪掉重建，決定不能跟著消失。"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from library_agent.db.models import PurchaseDecision

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def test_purchase_decision_roundtrip(isolated_db):
    with isolated_db() as s:
        s.add(PurchaseDecision(book_key="isbn:9780306406157", copies=3, suggested_at_decision=5, decided_at=NOW))
        s.commit()

    with isolated_db() as s:
        row = s.scalar(select(PurchaseDecision))
        assert (row.book_key, row.copies, row.suggested_at_decision, row.decided_at) == ("isbn:9780306406157", 3, 5, NOW)


def test_book_key_is_unique(isolated_db):
    with isolated_db() as s:
        s.add(PurchaseDecision(book_key="k", copies=1, suggested_at_decision=1, decided_at=NOW))
        s.commit()
        s.add(PurchaseDecision(book_key="k", copies=2, suggested_at_decision=1, decided_at=NOW))
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.parametrize("missing", ["book_key", "copies", "suggested_at_decision", "decided_at"])
def test_every_column_is_required(isolated_db, missing):
    values = dict(book_key="k", copies=1, suggested_at_decision=1, decided_at=NOW)
    values[missing] = None

    with isolated_db() as s:
        s.add(PurchaseDecision(**values))
        with pytest.raises(IntegrityError):
            s.commit()
