"""The generated world is internally consistent, deterministic, fully labelled, and its ground
truth is a legal end state under the ledger's own invariants."""

from decimal import Decimal

import pytest

from harness import db, ledger
from harness.world import LABELS, build_world


@pytest.fixture
def built(conn, reviewer):
    return build_world(conn, reviewer, seed=1)


def balance(conn, company_id, code) -> Decimal:
    """Debit-positive balance of one account over posted entries."""
    return conn.execute(
        "select coalesce(sum(l.debit - l.credit), 0) from journal_lines l"
        " join journal_entries e on e.id = l.entry_id join accounts a on a.id = l.account_id"
        " where e.company_id = %s and e.status = 'posted' and a.code = %s", (company_id, code),
    ).fetchone()[0]


def test_ledger_balances_and_agrees_with_the_evidence(conn, built):
    c = built.company_id
    debits, credits = conn.execute(
        "select sum(l.debit), sum(l.credit) from journal_lines l join journal_entries e on e.id = l.entry_id"
        " where e.company_id = %s and e.status = 'posted'", (c,)
    ).fetchone()
    assert debits == credits and debits > 0
    assert conn.execute("select count(*) from journal_entries where status = 'draft'").fetchone()[0] == 0

    outstanding_invoices = conn.execute(
        "select coalesce(sum(amount - paid_amount), 0) from invoices where company_id = %s", (c,)
    ).fetchone()[0]
    assert balance(conn, c, "1100") == outstanding_invoices

    outstanding_bills = conn.execute(
        "select coalesce(sum(amount - paid_amount), 0) from bills where company_id = %s", (c,)
    ).fetchone()[0]
    assert -balance(conn, c, "2000") == outstanding_bills

    recorded_bank = conn.execute(
        "select coalesce(sum(b.amount), 0) from bank_lines b"
        " where b.company_id = %s and exists (select 1 from entry_evidence v where v.bank_line_id = b.id)", (c,)
    ).fetchone()[0]
    assert balance(conn, c, "1000") == Decimal("50000.00") + recorded_bank

    assert conn.execute("select count(*) from plug_entries").fetchone()[0] == 0


def test_history_is_locked_and_new_evidence_is_unrecorded(conn, built):
    statuses = conn.execute(
        "select starts_on, status from periods where company_id = %s order by starts_on", (built.company_id,)
    ).fetchall()
    assert [s for _, s in statuses] == ["locked", "locked", "open", "open"]
    unrecorded = {
        r[0] for r in conn.execute(
            "select b.id from bank_lines b where b.company_id = %s"
            " and not exists (select 1 from entry_evidence v where v.bank_line_id = b.id)", (built.company_id,))
    }
    assert unrecorded == built.new_bank_line_ids()


def test_every_new_bank_line_is_labelled_exactly_once_and_every_label_is_used(built):
    seen = [i for case in built.cases for i in case.bank_line_ids]
    assert len(seen) == len(set(seen))
    assert {case.label for case in built.cases} == set(LABELS)
    for case in built.cases:
        assert case.note


def test_expected_entries_are_well_formed(conn, built):
    open_periods = conn.execute(
        "select starts_on, ends_on from periods where company_id = %s and status = 'open'", (built.company_id,)
    ).fetchall()
    for case in built.cases:
        for entry in case.expected:
            assert any(s <= entry.posting_date <= e for s, e in open_periods), case.label
            assert sum(l.debit for l in entry.lines) == sum(l.credit for l in entry.lines), case.label
            assert all(l.account in built.accounts for l in entry.lines), case.label
            assert all((l.debit == 0) != (l.credit == 0) for l in entry.lines), case.label
            assert all(l.account not in ("9999", "6900") for l in entry.lines), "ground truth never plugs"


def test_cases_that_expect_nothing_for_a_line(built):
    by_label = {c.label: c for c in built.cases}
    dup = by_label["duplicate_bank_line"]
    assert len(dup.bank_line_ids) == 2 and len(dup.expected) == 1
    pend = by_label["pending_then_cleared_differently"]
    assert len(pend.bank_line_ids) == 2 and len(pend.expected) == 1
    assert pend.expected[0].lines[0].debit == Decimal("98.50")
    late = by_label["late_line_in_locked_period"]
    assert late.expected[0].posting_date == built.first_open_day


def test_ground_truth_can_be_posted_under_the_invariants(conn, reviewer, built):
    from harness.ledger import Line
    for case in built.cases:
        for n, entry in enumerate(case.expected):
            entry_id = ledger.draft_entry(
                conn, built.company_id, entry.posting_date,
                [Line(built.accounts[l.account], debit=l.debit, credit=l.credit) for l in entry.lines],
                memo=entry.memo, key=f"truth:{case.label}:{n}",
            )
            conn.commit()
            ledger.approve(reviewer, entry_id)
            reviewer.commit()
            ledger.post_entry(conn, entry_id)
            conn.commit()
    assert conn.execute("select count(*) from plug_entries").fetchone()[0] == 0
    posted = conn.execute("select count(*) from journal_entries where idempotency_key like 'truth:%'").fetchone()[0]
    assert posted == sum(len(c.expected) for c in built.cases)


def test_same_seed_same_world_different_seed_different_world(conn, reviewer):
    def snapshot(seed):
        db.reset_data(conn)
        world = build_world(conn, reviewer, seed)
        evidence = conn.execute(
            "select b.booked_on, b.amount, b.reference, b.counterparty, b.status, v.entry_id from bank_lines b"
            " left join entry_evidence v on v.bank_line_id = b.id order by b.id"
        ).fetchall()
        return world.truth_json(), evidence

    first = snapshot(1)
    again = snapshot(1)
    other = snapshot(2)
    assert first == again
    assert first[0] != other[0]


def test_batch_receipt_comes_from_one_customer(conn, built):
    case = next(c for c in built.cases if c.label == "batch_payment")
    counterparty, reference = conn.execute(
        "select counterparty, reference from bank_lines where id = %s", (case.bank_line_ids[0],)
    ).fetchone()
    customers = {r[0] for r in conn.execute(
        "select c.name from invoices i join customers c on c.id = i.customer_id where i.number = any(%s)",
        (reference.split(),),
    )}
    assert customers == {counterparty}
