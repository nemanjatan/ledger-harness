"""Thin SQL wrappers over the sandbox ledger.

Nothing here commits. The caller owns the transaction, because the balance invariant is
checked at commit and the caller must see that outcome. Nothing here validates either: the
database is the only place invariants live, and these wrappers exist so tests and adapters do
not repeat INSERT statements. Who the actor is comes from the connection's login role.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg

ZERO = Decimal("0")


@dataclass(frozen=True)
class Line:
    account_id: int
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: str = ""


def create_company(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("insert into companies (name) values (%s) returning id", (name,)).fetchone()
    return row[0]


def add_account(
    conn: psycopg.Connection, company_id: int, code: str, name: str, kind: str, is_suspense: bool = False
) -> int:
    row = conn.execute(
        "insert into accounts (company_id, code, name, kind, is_suspense) values (%s, %s, %s, %s, %s) returning id",
        (company_id, code, name, kind, is_suspense),
    ).fetchone()
    return row[0]


def add_period(conn: psycopg.Connection, company_id: int, starts_on: date, ends_on: date) -> int:
    row = conn.execute(
        "insert into periods (company_id, starts_on, ends_on) values (%s, %s, %s) returning id",
        (company_id, starts_on, ends_on),
    ).fetchone()
    return row[0]


def lock_through(conn: psycopg.Connection, company_id: int, through: date) -> int:
    return conn.execute("select lock_through(%s, %s)", (company_id, through)).fetchone()[0]


def draft_entry(
    conn: psycopg.Connection,
    company_id: int,
    posting_date: date,
    lines: list[Line],
    memo: str = "",
    key: str | None = None,
) -> int:
    """Insert an entry and its lines in the current transaction. Commit decides if it stands.

    `key` is the idempotency key, unique per company. A caller that retries with the same key
    gets a unique violation instead of a second draft; a caller with no natural key gets a
    random one.
    """
    entry_id = conn.execute(
        "insert into journal_entries (company_id, posting_date, memo, idempotency_key)"
        " values (%s, %s, %s, %s) returning id",
        (company_id, posting_date, memo, key or uuid4().hex),
    ).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            "insert into journal_lines (entry_id, company_id, account_id, debit, credit, description)"
            " values (%s, %s, %s, %s, %s, %s)",
            [(entry_id, company_id, ln.account_id, ln.debit, ln.credit, ln.description) for ln in lines],
        )
    return entry_id


def approve(conn: psycopg.Connection, entry_id: int, note: str = "") -> int:
    """Approve a draft as the connection's role. The database refuses self-approval."""
    row = conn.execute(
        "insert into approvals (entry_id, note) values (%s, %s) returning id", (entry_id, note)
    ).fetchone()
    return row[0]


def withdraw_approval(conn: psycopg.Connection, entry_id: int) -> None:
    conn.execute("delete from approvals where entry_id = %s", (entry_id,))


def post_entry(conn: psycopg.Connection, entry_id: int) -> None:
    """Post a draft. Idempotent: posting a posted entry is a no-op."""
    conn.execute("select post_entry(%s)", (entry_id,))


def reverse_entry(
    conn: psycopg.Connection, entry_id: int, posting_date: date, memo: str = "", key: str | None = None
) -> int:
    """Draft the reversal of a posted entry: same lines, sides swapped. Returns the draft id."""
    return conn.execute(
        "select reverse_entry(%s, %s, %s, %s)", (entry_id, posting_date, key or uuid4().hex, memo)
    ).fetchone()[0]


def link_evidence(conn: psycopg.Connection, entry_id: int, bank_line_ids: list[int]) -> None:
    """Record that the entry records these bank lines. Frozen with the entry once posted."""
    company_id = conn.execute("select company_id from journal_entries where id = %s", (entry_id,)).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            "insert into entry_evidence (entry_id, company_id, bank_line_id) values (%s, %s, %s)",
            [(entry_id, company_id, b) for b in bank_line_ids],
        )


def entry_status(conn: psycopg.Connection, entry_id: int) -> str | None:
    row = conn.execute("select status from journal_entries where id = %s", (entry_id,)).fetchone()
    return row[0] if row else None


def audit_trail(conn: psycopg.Connection, table_name: str, row_id: int) -> list[tuple]:
    """(actor, action, old_row, new_row) in order, for one row of one table."""
    return conn.execute(
        "select actor, action, old_row, new_row from audit_log"
        " where table_name = %s and row_id = %s order by id",
        (table_name, row_id),
    ).fetchall()
