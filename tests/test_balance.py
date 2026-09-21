"""Invariant: every journal entry balances and has at least two lines, at every commit."""

from decimal import Decimal

import pytest
from psycopg.errors import CheckViolation

from harness.ledger import Line, draft_entry, entry_status
from tests.conftest import rejected_by


def test_balanced_entry_is_accepted(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")),
        Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()
    assert entry_status(conn, entry_id) == "draft"


def test_unbalanced_entry_is_rejected_at_commit(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")),
        Line(world.revenue, credit=Decimal("99.99")),
    ])
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "entry_balanced")
    assert entry_status(conn, entry_id) is None, "the whole transaction is gone, not just a line"


def test_many_lines_balance_as_a_whole(conn, world):
    draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("60.00")),
        Line(world.receivables, debit=Decimal("40.00")),
        Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()


def test_entry_with_no_lines_is_rejected(conn, world):
    draft_entry(conn, world.company_id, world.open_day, [])
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "entry_has_lines")


def test_entry_with_one_line_is_rejected(conn, world):
    # A single line can never balance, but the rule is stated on its own so the message says why.
    draft_entry(conn, world.company_id, world.open_day, [Line(world.cash, debit=Decimal("1.00"))])
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "entry_has_lines")


def test_balance_holds_when_a_line_is_added_later(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")),
        Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()
    conn.execute(
        "insert into journal_lines (entry_id, company_id, account_id, debit) values (%s, %s, %s, 5)",
        (entry_id, world.company_id, world.receivables),
    )
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "entry_balanced")
    n = conn.execute("select count(*) from journal_lines where entry_id = %s", (entry_id,)).fetchone()[0]
    assert n == 2


def test_balance_holds_when_a_line_is_deleted_later(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("60.00")),
        Line(world.receivables, debit=Decimal("40.00")),
        Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()
    conn.execute("delete from journal_lines where entry_id = %s and account_id = %s", (entry_id, world.receivables))
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "entry_balanced")


def test_balance_holds_when_an_amount_is_edited_later(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")),
        Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()
    conn.execute("update journal_lines set debit = 100.01 where entry_id = %s and debit > 0", (entry_id,))
    with pytest.raises(CheckViolation) as excinfo:
        conn.commit()
    assert rejected_by(excinfo, "entry_balanced")


def test_deleting_a_draft_with_its_lines_is_fine(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")),
        Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()
    conn.execute("delete from journal_lines where entry_id = %s", (entry_id,))
    conn.execute("delete from journal_entries where id = %s", (entry_id,))
    conn.commit()
    assert entry_status(conn, entry_id) is None


@pytest.mark.parametrize("debit, credit", [
    ("0", "0"),
    ("10.00", "10.00"),
    ("10.00", "0.01"),
    ("-10.00", "0"),
    ("0", "-10.00"),
])
def test_line_is_a_debit_or_a_credit_never_both_or_neither(conn, world, debit, credit):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [])
    with pytest.raises(CheckViolation):
        conn.execute(
            "insert into journal_lines (entry_id, company_id, account_id, debit, credit)"
            " values (%s, %s, %s, %s, %s)",
            (entry_id, world.company_id, world.cash, Decimal(debit), Decimal(credit)),
        )


def test_line_account_must_belong_to_the_entry_company(conn, world):
    from psycopg.errors import ForeignKeyViolation

    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")),
    ])
    with pytest.raises(ForeignKeyViolation):
        conn.execute(
            "insert into journal_lines (entry_id, company_id, account_id, credit) values (%s, %s, %s, 100)",
            (entry_id, world.company_id, world.other_cash),
        )


def test_line_cannot_claim_a_different_company_than_its_entry(conn, world):
    from psycopg.errors import ForeignKeyViolation

    entry_id = draft_entry(conn, world.company_id, world.open_day, [])
    with pytest.raises(ForeignKeyViolation):
        conn.execute(
            "insert into journal_lines (entry_id, company_id, account_id, credit) values (%s, %s, %s, 100)",
            (entry_id, world.other_company_id, world.other_cash),
        )


def test_amounts_come_back_as_decimal(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("0.10")),
        Line(world.revenue, credit=Decimal("0.10")),
    ])
    conn.commit()
    debit = conn.execute("select debit from journal_lines where entry_id = %s and debit > 0", (entry_id,)).fetchone()[0]
    assert debit == Decimal("0.10") and isinstance(debit, Decimal)
