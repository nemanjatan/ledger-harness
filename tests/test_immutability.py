"""Invariant: a posted entry, its lines and its approval never change. Two belts: triggers
(apply to everyone, the owner included) and grants (the agent role cannot even try most of it)."""

from decimal import Decimal

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege

from harness.ledger import Line, draft_entry, entry_status
from tests.conftest import posted_entry, rejected_by


def test_posted_entry_cannot_be_updated(conn, reviewer, world):
    entry_id = posted_entry(conn, reviewer, world)
    for statement in (
        "update journal_entries set memo = 'x' where id = %s",
        "update journal_entries set posting_date = posting_date + 1 where id = %s",
        "update journal_entries set status = 'draft', posted_at = null, posted_by = null where id = %s",
        "update journal_entries set company_id = company_id where id = %s",
    ):
        with pytest.raises(CheckViolation) as excinfo:
            conn.execute(statement, (entry_id,))
        assert rejected_by(excinfo, "posted_entry_immutable"), statement
        conn.rollback()


def test_posted_entry_cannot_be_deleted(conn, reviewer, world):
    entry_id = posted_entry(conn, reviewer, world)
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("delete from journal_entries where id = %s", (entry_id,))
    assert rejected_by(excinfo, "posted_entry_immutable")


def test_posted_lines_cannot_be_edited_deleted_or_added(conn, reviewer, world):
    entry_id = posted_entry(conn, reviewer, world)
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update journal_lines set debit = debit + 1 where entry_id = %s and debit > 0", (entry_id,))
    assert rejected_by(excinfo, "posted_entry_immutable")
    conn.rollback()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("delete from journal_lines where entry_id = %s", (entry_id,))
    assert rejected_by(excinfo, "posted_entry_immutable")
    conn.rollback()
    # A balanced pair would pass the balance check; immutability still refuses it.
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute(
            "insert into journal_lines (entry_id, company_id, account_id, debit, credit) values"
            " (%(e)s, %(c)s, %(cash)s, 5, 0), (%(e)s, %(c)s, %(rev)s, 0, 5)",
            {"e": entry_id, "c": world.company_id, "cash": world.cash, "rev": world.revenue},
        )
    assert rejected_by(excinfo, "posted_entry_immutable")


def test_a_line_cannot_be_moved_from_a_draft_into_a_posted_entry(conn, reviewer, world):
    posted_id = posted_entry(conn, reviewer, world)
    draft_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("1.00")), Line(world.revenue, credit=Decimal("1.00")),
    ])
    conn.commit()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update journal_lines set entry_id = %s where entry_id = %s", (posted_id, draft_id))
    assert rejected_by(excinfo, "posted_entry_immutable")


def test_unapproved_draft_is_still_editable(conn, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")), Line(world.revenue, credit=Decimal("100.00")),
    ])
    conn.commit()
    conn.execute("update journal_entries set memo = 'corrected' where id = %s", (entry_id,))
    conn.execute("update journal_lines set debit = 90 where entry_id = %s and debit > 0", (entry_id,))
    conn.execute("update journal_lines set credit = 90 where entry_id = %s and credit > 0", (entry_id,))
    conn.commit()
    assert entry_status(conn, entry_id) == "draft"


def test_agent_cannot_write_status_or_actor_columns_on_insert(agent, world):
    for columns, values in (
        ("company_id, posting_date, idempotency_key, status", "%s, %s, 'k', 'posted'"),
        ("company_id, posting_date, idempotency_key, created_by", "%s, %s, 'k', 'someone'"),
        ("company_id, posting_date, idempotency_key, posted_at", "%s, %s, 'k', now()"),
    ):
        with pytest.raises(InsufficientPrivilege):
            agent.execute(
                f"insert into journal_entries ({columns}) values ({values})", (world.company_id, world.open_day)
            )
        agent.rollback()


def test_agent_cannot_touch_provenance_columns_on_update(conn, agent, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("1.00")), Line(world.revenue, credit=Decimal("1.00")),
    ])
    conn.commit()
    for statement in (
        "update journal_entries set created_by = 'someone' where id = %s",
        "update journal_entries set posted_at = now() where id = %s",
        "update journal_entries set idempotency_key = 'other' where id = %s",
        "update journal_lines set entry_id = entry_id where entry_id = %s",
    ):
        with pytest.raises(InsufficientPrivilege):
            agent.execute(statement, (entry_id,))
        agent.rollback()


def test_agent_cannot_write_reference_data(agent, world):
    for statement in (
        "insert into companies (name) values ('Shell Co')",
        "insert into accounts (company_id, code, name, kind) values (%s, '1', 'x', 'asset')",
        "update accounts set is_suspense = false where company_id = %s",
        "insert into periods (company_id, starts_on, ends_on) values (%s, '2027-01-01', '2027-01-31')",
        "update periods set status = 'open' where company_id = %s",
    ):
        with pytest.raises(InsufficientPrivilege):
            agent.execute(statement, (world.company_id,) if "%s" in statement else None)
        agent.rollback()
