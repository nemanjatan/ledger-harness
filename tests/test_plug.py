"""Rule: a posted entry that touches a suspense-flagged account is reported as a plug. The
database reports; it does not block, because posting to suspense is legal in real books."""

from decimal import Decimal

from harness.ledger import Line, approve, draft_entry, post_entry
from tests.conftest import posted_entry


def plugs(conn, world):
    return conn.execute(
        "select entry_id, suspense_amount from plug_entries where company_id = %s order by entry_id",
        (world.company_id,),
    ).fetchall()


def test_entry_into_suspense_is_reported(conn, reviewer, world):
    entry_id = draft_entry(conn, world.company_id, world.open_day, [
        Line(world.cash, debit=Decimal("12.34"), description="unexplained bank difference"),
        Line(world.suspense, credit=Decimal("12.34")),
    ])
    conn.commit()
    approve(reviewer, entry_id)
    reviewer.commit()
    assert plugs(conn, world) == []                     # a draft is not a plug yet
    post_entry(conn, entry_id)
    conn.commit()
    assert plugs(conn, world) == [(entry_id, Decimal("12.34"))]


def test_ordinary_entries_are_not_reported(conn, reviewer, world):
    posted_entry(conn, reviewer, world)
    assert plugs(conn, world) == []


def test_agent_can_read_the_rule_but_not_move_the_flag(agent, world):
    import pytest
    from psycopg.errors import InsufficientPrivilege
    agent.execute("select * from plug_entries").fetchall()
    with pytest.raises(InsufficientPrivilege):
        agent.execute("update accounts set is_suspense = false where id = %s", (world.suspense,))
