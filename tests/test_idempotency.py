"""Invariant: one idempotency key per company creates one entry, however often it is sent."""

import pytest
from psycopg.errors import NotNullViolation, UniqueViolation

from harness.ledger import Line, draft_entry
from tests.conftest import balanced_draft


def test_same_key_twice_creates_one_entry(conn, world):
    balanced_draft(conn, world, key="bank-line-42")
    with pytest.raises(UniqueViolation):
        balanced_draft(conn, world, key="bank-line-42")
    conn.rollback()
    n = conn.execute("select count(*) from journal_entries where idempotency_key = 'bank-line-42'").fetchone()[0]
    assert n == 1


def test_key_is_scoped_to_the_company(conn, world):
    from decimal import Decimal
    balanced_draft(conn, world, key="bank-line-42")
    draft_entry(conn, world.other_company_id, world.open_day, [
        Line(world.other_cash, debit=Decimal("1.00")), Line(world.other_cash, credit=Decimal("1.00")),
    ], key="bank-line-42")
    conn.commit()


def test_key_is_required(conn, world):
    with pytest.raises(NotNullViolation):
        conn.execute(
            "insert into journal_entries (company_id, posting_date) values (%s, %s)",
            (world.company_id, world.open_day),
        )
