"""Invariant: the only correction to a posted entry is a reversal that mirrors it exactly."""

from datetime import date
from decimal import Decimal

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from harness.ledger import Line, approve, draft_entry, entry_status, post_entry, reverse_entry
from tests.conftest import posted_entry, rejected_by


def test_reverse_entry_drafts_the_mirror_and_it_can_be_posted(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world, amount="123.45")
    reversal = reverse_entry(conn, original, world.later_open_day, memo="posted twice")
    conn.commit()
    lines = conn.execute(
        "select account_id, debit, credit from journal_lines where entry_id = %s order by account_id", (reversal,)
    ).fetchall()
    assert lines == [(world.cash, Decimal("0.00"), Decimal("123.45")), (world.revenue, Decimal("123.45"), Decimal("0.00"))]
    approve(reviewer, reversal)
    reviewer.commit()
    post_entry(conn, reversal)
    conn.commit()
    assert entry_status(conn, reversal) == "posted"


def test_hand_made_reversal_with_a_wrong_amount_is_rejected(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world, amount="100.00")
    conn.execute(
        "insert into journal_entries (company_id, posting_date, idempotency_key, reverses_entry_id)"
        " values (%s, %s, 'rev', %s)", (world.company_id, world.later_open_day, original),
    )
    reversal = conn.execute("select id from journal_entries where idempotency_key = 'rev'").fetchone()[0]
    conn.execute(
        "insert into journal_lines (entry_id, company_id, account_id, debit, credit) values"
        " (%(e)s, %(c)s, %(cash)s, 0, 90), (%(e)s, %(c)s, %(rev)s, 90, 0)",
        {"e": reversal, "c": world.company_id, "cash": world.cash, "rev": world.revenue},
    )
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "reversal_mirrors_original")


def test_reversal_with_the_right_totals_but_wrong_account_is_rejected(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world, amount="100.00")
    reversal = draft_entry(conn, world.company_id, world.later_open_day, [
        Line(world.receivables, credit=Decimal("100.00")),     # should be cash
        Line(world.revenue, debit=Decimal("100.00")),
    ])
    conn.execute("update journal_entries set reverses_entry_id = %s where id = %s", (original, reversal))
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "reversal_mirrors_original")


def test_reversal_of_a_draft_is_rejected(conn, world):
    from tests.conftest import balanced_draft
    draft = balanced_draft(conn, world)
    reverse_entry(conn, draft, world.later_open_day)
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "reversal_of_posted_entry")


def test_reversal_dated_before_the_original_is_rejected(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world, posting_date=world.later_open_day)
    reverse_entry(conn, original, world.open_day)
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "reversal_not_before_original")


def test_an_entry_is_reversed_at_most_once(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world)
    reverse_entry(conn, original, world.later_open_day)
    conn.commit()
    with pytest.raises(UniqueViolation):
        reverse_entry(conn, original, world.later_open_day)


def test_reversal_cannot_cross_companies(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world)
    with pytest.raises(ForeignKeyViolation):
        conn.execute(
            "insert into journal_entries (company_id, posting_date, idempotency_key, reverses_entry_id)"
            " values (%s, %s, 'x', %s)", (world.other_company_id, world.later_open_day, original),
        )


def test_reversal_lines_are_frozen_once_approved_like_any_draft(conn, reviewer, world):
    original = posted_entry(conn, reviewer, world)
    reversal = reverse_entry(conn, original, world.later_open_day)
    conn.commit()
    approve(reviewer, reversal)
    reviewer.commit()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("delete from journal_lines where entry_id = %s", (reversal,))
    assert rejected_by(excinfo, "approved_entry_immutable")
