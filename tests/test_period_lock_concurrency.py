"""The period check and lock_through() cannot interleave: posting takes a share lock on the
period row, so a concurrent lock waits until the posting transaction commits or rolls back."""

from datetime import date

import pytest
from psycopg.errors import LockNotAvailable

from harness import db
from harness.ledger import approve, entry_status, post_entry
from tests.conftest import balanced_draft


def approved_draft(conn, reviewer, world):
    entry_id = balanced_draft(conn, world)
    approve(reviewer, entry_id)
    reviewer.commit()
    return entry_id


def test_lock_through_waits_for_an_in_flight_posting(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world)

    post_entry(conn, entry_id)          # not committed: the period row is now share-locked

    with db.connect() as other:
        other.execute("set lock_timeout = '200ms'")
        with pytest.raises(LockNotAvailable):
            other.execute("select lock_through(%s, %s)", (world.company_id, date(2026, 2, 28)))
        other.rollback()

        conn.commit()                   # posting lands first; the lock can now proceed
        assert other.execute("select lock_through(%s, %s)", (world.company_id, date(2026, 2, 28))).fetchone()[0] == 1
        other.commit()

    assert entry_status(conn, entry_id) == "posted"
    status = conn.execute(
        "select status from periods where company_id = %s and starts_on = %s", (world.company_id, date(2026, 2, 1))
    ).fetchone()[0]
    assert status == "locked"


def test_posting_waits_for_an_in_flight_lock(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world)

    with db.connect() as other:
        other.execute("select lock_through(%s, %s)", (world.company_id, date(2026, 2, 28)))   # not committed

        conn.execute("set lock_timeout = '200ms'")
        with pytest.raises(LockNotAvailable):
            post_entry(conn, entry_id)
        conn.rollback()
        other.commit()

    # Once the lock is committed the posting is refused outright, never silently accepted.
    from psycopg.errors import CheckViolation
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)
    assert excinfo.value.diag.constraint_name == "period_open"
