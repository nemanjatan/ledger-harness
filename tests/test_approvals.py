"""Invariant: posting needs an approval by someone other than the drafter, approvals are
immutable, and an approved draft is frozen until it is posted."""

from decimal import Decimal

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation

from harness import db
from harness.ledger import Line, approve, draft_entry, entry_status, post_entry, withdraw_approval
from tests.conftest import balanced_draft, posted_entry, rejected_by


def test_posting_without_approval_is_rejected(conn, world):
    entry_id = balanced_draft(conn, world)
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)
    assert rejected_by(excinfo, "entry_approved")


def test_agent_drafts_reviewer_approves_agent_posts(agent, reviewer, world):
    entry_id = draft_entry(agent, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("100.00")), Line(world.revenue, credit=Decimal("100.00")),
    ])
    agent.commit()
    approve(reviewer, entry_id, note="matches invoice 17")
    reviewer.commit()
    post_entry(agent, entry_id)
    agent.commit()
    created_by, posted_by, approved_by = agent.execute(
        "select e.created_by, e.posted_by, a.approved_by from journal_entries e"
        " join approvals a on a.entry_id = e.id where e.id = %s", (entry_id,)
    ).fetchone()
    assert (created_by, posted_by, approved_by) == (db.AGENT, db.AGENT, db.REVIEWER)


def test_drafter_cannot_approve_own_draft(conn, world):
    entry_id = balanced_draft(conn, world)     # drafted by the owner
    with pytest.raises(CheckViolation) as excinfo:
        approve(conn, entry_id)                # approved by the owner
    assert rejected_by(excinfo, "approver_not_creator")


def test_agent_cannot_insert_an_approval_at_all(agent, world):
    entry_id = draft_entry(agent, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("1.00")), Line(world.revenue, credit=Decimal("1.00")),
    ])
    agent.commit()
    with pytest.raises(InsufficientPrivilege):
        approve(agent, entry_id)


def test_approver_identity_is_the_login_role_not_a_supplied_value(conn, reviewer, world):
    entry_id = balanced_draft(conn, world)
    reviewer.execute(
        "insert into approvals (entry_id, note) values (%s, 'x')", (entry_id,)
    )
    reviewer.commit()
    # Even the owner cannot spell a different approver: the column is overwritten with the
    # login role, which makes this a self-approval by the owner, and that is refused.
    other = balanced_draft(conn, world)
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("insert into approvals (entry_id, approved_by) values (%s, 'auditor')", (other,))
    assert rejected_by(excinfo, "approver_not_creator")
    conn.rollback()
    approved_by = conn.execute("select approved_by from approvals where entry_id = %s", (entry_id,)).fetchone()[0]
    assert approved_by == db.REVIEWER


def test_poster_identity_is_set_by_the_ledger(conn, reviewer, world):
    entry_id = posted_entry(conn, reviewer, world)
    posted_by, posted_at = conn.execute(
        "select posted_by, posted_at from journal_entries where id = %s", (entry_id,)
    ).fetchone()
    assert posted_by == "ledger" and posted_at is not None


def test_one_approval_per_entry(conn, reviewer, world):
    entry_id = balanced_draft(conn, world)
    approve(reviewer, entry_id)
    with pytest.raises(UniqueViolation):
        approve(reviewer, entry_id)


def test_approval_cannot_be_edited(conn, reviewer, world):
    entry_id = balanced_draft(conn, world)
    approve(reviewer, entry_id)
    reviewer.commit()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update approvals set note = 'changed' where entry_id = %s", (entry_id,))
    assert rejected_by(excinfo, "approval_immutable")


def test_approved_draft_is_frozen_until_withdrawn(conn, reviewer, world):
    entry_id = balanced_draft(conn, world)
    approve(reviewer, entry_id)
    reviewer.commit()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update journal_entries set memo = 'x' where id = %s", (entry_id,))
    assert rejected_by(excinfo, "approved_entry_immutable")
    conn.rollback()
    with pytest.raises(CheckViolation) as excinfo:
        conn.execute("update journal_lines set debit = 1 where entry_id = %s and debit > 0", (entry_id,))
    assert rejected_by(excinfo, "approved_entry_immutable")
    conn.rollback()

    withdraw_approval(reviewer, entry_id)
    reviewer.commit()
    conn.execute("update journal_entries set memo = 'x' where id = %s", (entry_id,))
    conn.commit()
    with pytest.raises(CheckViolation) as excinfo:
        post_entry(conn, entry_id)              # and it needs approving again
    assert rejected_by(excinfo, "entry_approved")


def test_approval_of_a_posted_entry_cannot_be_withdrawn(conn, reviewer, world):
    entry_id = posted_entry(conn, reviewer, world)
    with pytest.raises(CheckViolation) as excinfo:
        withdraw_approval(reviewer, entry_id)
    assert rejected_by(excinfo, "posted_entry_immutable")


def test_posted_entry_cannot_be_approved(conn, reviewer, world):
    # Normally unreachable, since a posted entry already carries its approval. The owner
    # removes it with the guard switched off to show the rule holds on its own.
    entry_id = posted_entry(conn, reviewer, world)
    conn.execute("alter table approvals disable trigger approvals_guard")
    conn.execute("delete from approvals where entry_id = %s", (entry_id,))
    conn.execute("alter table approvals enable trigger approvals_guard")
    conn.commit()
    with pytest.raises(CheckViolation) as excinfo:
        approve(reviewer, entry_id)
    assert rejected_by(excinfo, "approval_before_posting")


def test_posting_is_idempotent(conn, reviewer, world):
    entry_id = posted_entry(conn, reviewer, world)
    first = conn.execute("select posted_at from journal_entries where id = %s", (entry_id,)).fetchone()[0]
    post_entry(conn, entry_id)
    conn.commit()
    second = conn.execute("select posted_at from journal_entries where id = %s", (entry_id,)).fetchone()[0]
    assert entry_status(conn, entry_id) == "posted" and first == second


def test_posting_a_missing_entry_is_an_error_not_a_silent_noop(conn, world):
    from psycopg.errors import NoDataFound
    with pytest.raises(NoDataFound):
        post_entry(conn, 999_999)
