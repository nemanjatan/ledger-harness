"""Fixtures. The schema is applied once per session; every test starts from empty tables and
a small world: one company, five accounts, three monthly periods with January locked.

Three connections, one per login role, because identity is the login role:
  conn      the owner, who builds the world and can do anything the triggers allow
  agent     ledger_agent, who drafts and posts
  reviewer  ledger_reviewer, who approves
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from harness import db, ledger


def rejected_by(excinfo, invariant: str) -> bool:
    return excinfo.value.diag.constraint_name == invariant


@pytest.fixture(scope="session")
def schema():
    with db.connect() as conn:
        others = conn.execute(
            "select count(*) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()"
        ).fetchone()[0]
        if others:
            pytest.exit(f"{others} other session(s) are using the database (a model run?); "
                        "the suite would drop the schema under them. Wait for the run to finish.", returncode=3)
        db.reset_schema(conn)


@pytest.fixture
def conn(schema):
    with db.connect() as conn:
        db.reset_data(conn)
        yield conn
        conn.rollback()


@pytest.fixture
def agent(conn):
    with db.connect(db.AGENT) as c:
        yield c
        c.rollback()


@pytest.fixture
def reviewer(conn):
    with db.connect(db.REVIEWER) as c:
        yield c
        c.rollback()


@dataclass(frozen=True)
class World:
    company_id: int
    cash: int
    receivables: int
    revenue: int
    expenses: int
    suspense: int
    other_company_id: int
    other_cash: int
    locked_day: date = date(2026, 1, 15)
    open_day: date = date(2026, 2, 15)
    later_open_day: date = date(2026, 3, 15)
    no_period_day: date = date(2026, 6, 1)


@pytest.fixture
def world(conn) -> World:
    company_id = ledger.create_company(conn, "Acme Widgets")
    accounts = {
        "cash": ledger.add_account(conn, company_id, "1000", "Cash at bank", "asset"),
        "receivables": ledger.add_account(conn, company_id, "1100", "Accounts receivable", "asset"),
        "revenue": ledger.add_account(conn, company_id, "4000", "Sales", "revenue"),
        "expenses": ledger.add_account(conn, company_id, "6000", "Office expenses", "expense"),
        "suspense": ledger.add_account(conn, company_id, "9999", "Suspense", "asset", is_suspense=True),
    }
    for month in (1, 2, 3):
        ledger.add_period(conn, company_id, date(2026, month, 1), date(2026, month + 1, 1) - date.resolution)
    ledger.lock_through(conn, company_id, date(2026, 1, 31))

    other_company_id = ledger.create_company(conn, "Other Co")
    other_cash = ledger.add_account(conn, other_company_id, "1000", "Cash", "asset")
    conn.commit()
    return World(company_id, **accounts, other_company_id=other_company_id, other_cash=other_cash)


def balanced_draft(conn, world, posting_date=None, amount="100.00", key=None) -> int:
    """A plain cash sale, committed. Returns the draft's id."""
    entry_id = ledger.draft_entry(conn, world.company_id, posting_date or world.open_day, [
        ledger.Line(world.cash, debit=Decimal(amount)),
        ledger.Line(world.revenue, credit=Decimal(amount)),
    ], key=key)
    conn.commit()
    return entry_id


def posted_entry(conn, reviewer, world, posting_date=None, amount="100.00") -> int:
    """A cash sale drafted by `conn`, approved by `reviewer`, posted by `conn`, committed."""
    entry_id = balanced_draft(conn, world, posting_date, amount)
    ledger.approve(reviewer, entry_id)
    reviewer.commit()
    ledger.post_entry(conn, entry_id)
    conn.commit()
    return entry_id
