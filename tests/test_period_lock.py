"""Invariant: a posted entry's date falls in an open period, and periods cannot be moved."""

from datetime import date

import pytest
from psycopg.errors import CheckViolation, ExclusionViolation

from harness.ledger import add_period, approve, entry_status, lock_through, post_entry
from tests.conftest import balanced_draft, rejected_by


def approved_draft(conn, reviewer, world, posting_date):
    entry_id = balanced_draft(conn, world, posting_date)
    approve(reviewer, entry_id)
    reviewer.commit()
    return entry_id


def test_posting_into_an_open_period_works(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world, world.open_day)
    post_entry(conn, entry_id)
    conn.commit()
    assert entry_status(conn, entry_id) == "posted"


def test_posting_into_a_locked_period_is_rejected(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world, world.locked_day)
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)
    assert rejected_by(excinfo, "period_open")


def test_posting_outside_any_period_is_rejected(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world, world.no_period_day)
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)
    assert rejected_by(excinfo, "posting_date_in_period")


def test_inserting_directly_as_posted_is_refused(conn, world):
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute(
            "insert into journal_entries (company_id, posting_date, idempotency_key, status, posted_at, posted_by)"
            " values (%s, %s, 'k', 'posted', now(), 'ledger')",
            (world.company_id, world.open_day),
        )
    assert rejected_by(excinfo, "entry_starts_as_draft")


def test_a_draft_may_sit_in_a_locked_period_but_cannot_be_posted(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world, world.locked_day)
    assert entry_status(conn, entry_id) == "draft"
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)
    assert rejected_by(excinfo, "period_open")


def test_locking_after_the_draft_still_blocks_posting(conn, reviewer, world):
    entry_id = approved_draft(conn, reviewer, world, world.open_day)
    assert lock_through(conn, world.company_id, date(2026, 2, 28)) == 1
    conn.commit()
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)
    assert rejected_by(excinfo, "period_open")


def test_moving_a_posted_entry_into_a_locked_period_is_rejected(conn, reviewer, world):
    # A posted entry cannot move at all; immutability answers before the period check.
    entry_id = approved_draft(conn, reviewer, world, world.open_day)
    post_entry(conn, entry_id)
    conn.commit()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update journal_entries set posting_date = %s where id = %s", (world.locked_day, entry_id))
    assert rejected_by(excinfo, "posted_entry_immutable")


def test_lock_through_locks_only_periods_ending_on_or_before(conn, world):
    # January is already locked by the fixture. Lock through 15 March: February goes, March stays.
    assert lock_through(conn, world.company_id, date(2026, 3, 15)) == 1
    rows = conn.execute(
        "select starts_on, status from periods where company_id = %s order by starts_on", (world.company_id,)
    ).fetchall()
    assert [s for _, s in rows] == ["locked", "locked", "open"]


def test_lock_through_is_idempotent(conn, world):
    assert lock_through(conn, world.company_id, date(2026, 1, 31)) == 0


def test_lock_through_does_not_touch_other_companies(conn, world):
    add_period(conn, world.other_company_id, date(2026, 1, 1), date(2026, 1, 31))
    lock_through(conn, world.company_id, date(2026, 12, 31))
    status = conn.execute(
        "select status from periods where company_id = %s", (world.other_company_id,)
    ).fetchone()[0]
    assert status == "open"


def test_periods_of_one_company_cannot_overlap(conn, world):
    with pytest.raises(ExclusionViolation):
        add_period(conn, world.company_id, date(2026, 3, 20), date(2026, 4, 30))


def test_period_dates_cannot_change(conn, world):
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute(
            "update periods set ends_on = ends_on + 1 where company_id = %s and starts_on = %s",
            (world.company_id, date(2026, 3, 1)),
        )
    assert rejected_by(excinfo, "period_dates_fixed")


def test_periods_cannot_be_deleted(conn, world):
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("delete from periods where company_id = %s", (world.company_id,))
    assert rejected_by(excinfo, "period_never_deleted")


def test_period_may_be_reopened(conn, reviewer, world):
    # Reopening is allowed; the audit log records who did it (see test_audit_log.py).
    conn.execute(
        "update periods set status = 'open' where company_id = %s and starts_on = %s",
        (world.company_id, date(2026, 1, 1)),
    )
    conn.commit()
    entry_id = approved_draft(conn, reviewer, world, world.locked_day)
    post_entry(conn, entry_id)
    conn.commit()
    assert entry_status(conn, entry_id) == "posted"
