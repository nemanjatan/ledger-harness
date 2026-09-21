"""Invariant: every change to entries, lines, approvals and periods lands in an append-only
audit log with the login role that made it."""

from datetime import date

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege

from harness import db
from harness.ledger import Line, approve, audit_trail, draft_entry, post_entry
from tests.conftest import rejected_by


def test_the_life_of_an_entry_is_recorded_with_actors(agent, reviewer, conn, world):
    entry_id = draft_entry(agent, world.company_id, world.open_day, [
        Line(world.cash, debit=1), Line(world.revenue, credit=1),
    ])
    agent.commit()
    approval_id = approve(reviewer, entry_id)
    reviewer.commit()
    post_entry(agent, entry_id)
    agent.commit()

    trail = audit_trail(conn, "journal_entries", entry_id)
    assert [(actor, action) for actor, action, _, _ in trail] == [(db.AGENT, "INSERT"), (db.AGENT, "UPDATE")]
    _, _, old, new = trail[1]
    assert (old["status"], new["status"], new["posted_by"]) == ("draft", "posted", db.AGENT)

    assert [(a, act) for a, act, _, _ in audit_trail(conn, "approvals", approval_id)] == [(db.REVIEWER, "INSERT")]
    n_lines = conn.execute(
        "select count(*) from audit_log where table_name = 'journal_lines' and actor = %s", (db.AGENT,)
    ).fetchone()[0]
    assert n_lines == 2


def test_period_reopen_is_recorded(conn, world):
    period_id = conn.execute(
        "select id from periods where company_id = %s and starts_on = %s", (world.company_id, date(2026, 1, 1))
    ).fetchone()[0]
    conn.execute("update periods set status = 'open' where id = %s", (period_id,))
    conn.commit()
    trail = audit_trail(conn, "periods", period_id)
    actor, action, old, new = trail[-1]
    assert (actor, action, old["status"], new["status"]) == ("ledger", "UPDATE", "locked", "open")


def test_audit_rows_cannot_be_changed_or_removed(conn, world):
    row_id = conn.execute("select min(id) from audit_log").fetchone()[0]
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update audit_log set actor = 'nobody' where id = %s", (row_id,))
    assert rejected_by(excinfo, "audit_log_append_only")
    conn.rollback()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("delete from audit_log where id = %s", (row_id,))
    assert rejected_by(excinfo, "audit_log_append_only")


def test_roles_cannot_write_audit_rows_directly(agent, reviewer, world):
    for c in (agent, reviewer):
        with pytest.raises(InsufficientPrivilege):
            c.execute(
                "insert into audit_log (actor, table_name, row_id, action) values ('ledger_reviewer', 'approvals', 1, 'INSERT')"
            )
        c.rollback()


def test_a_rolled_back_change_leaves_no_audit_row(conn, world):
    before = conn.execute("select count(*) from audit_log").fetchone()[0]
    draft_entry(conn, world.company_id, world.open_day, [Line(world.cash, debit=1)])   # unbalanced
    with pytest.raises(CheckViolation):
        conn.commit()
    after = conn.execute("select count(*) from audit_log").fetchone()[0]
    assert after == before
