"""Synthetic world generator.

A world is one small company with four months of history already in the ledger, plus a set of
new, unrecorded bank lines and the ground truth for them: which entries a careful bookkeeper
would post, dated when, or nothing at all. Every case is labelled with the failure class it
exercises, named after what practitioners report.

Deterministic from a seed. Evidence goes into the database (the agent reads it). Ground truth
stays in Python and JSON and is never written to a table, so no read tool can leak it.

History is written through the same path the agent uses: drafted by the owner, approved by
the reviewer role, posted. The database invariants apply to the generator too.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import psycopg

from harness import ledger
from harness.ledger import Line

COMPANY = "Acme Widgets Ltd"
SISTER = "Beta Ltd"
OPENING_CASH = Decimal("50000.00")
FIRST_DAY = date(2026, 1, 1)
LOCK_THROUGH = date(2026, 2, 28)
FIRST_OPEN_DAY = date(2026, 3, 1)
TODAY = date(2026, 4, 30)

# code, name, kind, is_suspense
CHART = [
    ("1000", "Cash at bank", "asset", False),
    ("1100", "Accounts receivable", "asset", False),
    ("2000", "Accounts payable", "liability", False),
    ("2200", f"Due to {SISTER} (intercompany)", "liability", False),
    ("3000", "Owner's equity", "equity", False),
    ("4000", "Sales", "revenue", False),
    ("6000", "Office expenses", "expense", False),
    ("6100", "Software subscriptions", "expense", False),
    ("6200", "Bank charges", "expense", False),
    ("6300", "Payment processing fees", "expense", False),
    ("6900", "Miscellaneous expense", "expense", True),
    ("9999", "Suspense", "asset", True),
]

CUSTOMERS = ["Northwind Traders", "Harbour Books", "Kestrel Studio", "Pinecrest Dental", "Orbital Coffee", "Larkspur Events"]
# name, default expense account
VENDORS = [
    ("GitHub", "6100"), ("Notion Labs", "6100"), ("Cloudwave Hosting", "6100"),
    ("Staples", "6000"), ("Courier Express", "6000"), ("Office Greens", "6000"), ("Amazon", "6000"),
]

LABELS = {
    # ordinary
    "invoice_paid_exactly": "A customer pays one invoice in full.",
    "bill_paid_exactly": "The company pays one bill in full.",
    "direct_expense": "A card payment to a known vendor with no bill; the vendor has a default account.",
    "bank_fee": "The bank's monthly fee; history shows how it has been booked before.",
    # failure classes practitioners report
    "part_payment": "A customer pays part of an invoice; the rest stays receivable.",
    "batch_payment": "One receipt settles several invoices.",
    "fee_net_payout": "A card processor pays out gross collections net of its fees; the payout report says which invoices.",
    "intercompany_transfer": "Funding from the sister company is a liability to it, not revenue.",
    "bill_after_bank_line": "The vendor was paid before its bill was received; the bill is already in payables.",
    "duplicate_bank_line": "The bank feed delivered one receipt twice with a different reference.",
    "pending_then_cleared_differently": "A pending card line clears two days later at a different amount.",
    "late_line_in_locked_period": "A line dated in a locked period; it goes into the first open period, never the locked one.",
}


@dataclass(frozen=True)
class ExpectedLine:
    account: str                 # account code
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


@dataclass(frozen=True)
class ExpectedEntry:
    posting_date: date
    lines: tuple[ExpectedLine, ...]
    memo: str


@dataclass(frozen=True)
class Case:
    label: str
    bank_line_ids: tuple[int, ...]
    expected: tuple[ExpectedEntry, ...]     # empty means: post nothing
    note: str


@dataclass
class World:
    seed: int
    company_id: int
    accounts: dict[str, int]                # code -> account id
    today: date
    first_open_day: date
    history_max_entry_id: int               # entries above this id were made after the build
    cases: list[Case] = field(default_factory=list)

    def new_bank_line_ids(self) -> set[int]:
        return {i for c in self.cases for i in c.bank_line_ids}

    def truth_json(self) -> str:
        payload = {
            "seed": self.seed,
            "company_id": self.company_id,
            "today": self.today,
            "cases": [asdict(c) for c in self.cases],
        }
        return json.dumps(payload, default=str, indent=2)


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class _Builder:
    def __init__(self, owner: psycopg.Connection, reviewer: psycopg.Connection, seed: int, labels: list[str]):
        self.owner = owner
        self.reviewer = reviewer
        self.rng = random.Random(seed)
        self.seed = seed
        self.labels = labels
        self.accounts: dict[str, int] = {}
        self.company_id = 0
        self.invoices: list[dict] = []
        self.bills: list[dict] = []
        self.cases: list[Case] = []

    # --- primitives -------------------------------------------------------------------

    def amount(self, lo: int, hi: int) -> Decimal:
        return money(Decimal(self.rng.randint(lo * 100, hi * 100)) / 100)

    def day_in(self, year: int, month: int, lo: int = 1, hi: int = 28) -> date:
        return date(year, month, self.rng.randint(lo, hi))

    def post(self, posting_date: date, lines: list[tuple[str, Decimal, Decimal]], memo: str, key: str,
             bank_line_ids: list[int] = ()) -> int:
        """Draft (linking evidence), approve, post, through the same rules an agent faces."""
        entry_id = ledger.draft_entry(
            self.owner, self.company_id, posting_date,
            [Line(self.accounts[code], debit=d, credit=c) for code, d, c in lines],
            memo=memo, key=key,
        )
        ledger.link_evidence(self.owner, entry_id, list(bank_line_ids))
        self.owner.commit()
        ledger.approve(self.reviewer, entry_id, note="history")
        self.reviewer.commit()
        ledger.post_entry(self.owner, entry_id)
        self.owner.commit()
        return entry_id

    def bank_line(self, booked_on: date, amount: Decimal, reference: str, counterparty: str,
                  status: str = "cleared") -> int:
        row = self.owner.execute(
            "insert into bank_lines (company_id, booked_on, amount, reference, counterparty, status)"
            " values (%s, %s, %s, %s, %s, %s) returning id",
            (self.company_id, booked_on, amount, reference, counterparty, status),
        ).fetchone()
        self.owner.commit()
        return row[0]

    def case(self, label: str, bank_line_ids: list[int], expected: list[ExpectedEntry], note: str = "") -> None:
        self.cases.append(Case(label, tuple(bank_line_ids), tuple(expected), note or LABELS[label]))

    # --- world ------------------------------------------------------------------------

    def build(self) -> World:
        self.company_id = ledger.create_company(self.owner, COMPANY)
        ledger.create_company(self.owner, SISTER)
        for code, name, kind, is_suspense in CHART:
            self.accounts[code] = ledger.add_account(self.owner, self.company_id, code, name, kind, is_suspense)
        for month in (1, 2, 3, 4):
            last = date(2026, month + 1, 1) - timedelta(days=1)
            ledger.add_period(self.owner, self.company_id, date(2026, month, 1), last)
        self.customers = {
            name: self.owner.execute(
                "insert into customers (company_id, name) values (%s, %s) returning id", (self.company_id, name)
            ).fetchone()[0]
            for name in CUSTOMERS
        }
        self.vendors = {}
        self.vendor_account = {}
        for name, code in VENDORS:
            self.vendors[name] = self.owner.execute(
                "insert into vendors (company_id, name, expense_account_id) values (%s, %s, %s) returning id",
                (self.company_id, name, self.accounts[code]),
            ).fetchone()[0]
            self.vendor_account[name] = code
        self.owner.commit()

        self.post(FIRST_DAY, [("1000", OPENING_CASH, Decimal(0)), ("3000", Decimal(0), OPENING_CASH)],
                  "Opening balance", "opening")
        self.issue_invoices()
        self.receive_bills()
        self.settle_history()
        self.recurring_history()
        ledger.lock_through(self.owner, self.company_id, LOCK_THROUGH)
        self.owner.commit()
        self.new_evidence()
        history_max = self.owner.execute("select max(id) from journal_entries").fetchone()[0]
        self.owner.commit()     # never leave the owner idle in a transaction holding locks
        return World(self.seed, self.company_id, dict(self.accounts), TODAY, FIRST_OPEN_DAY, history_max, self.cases)

    def issue_invoices(self) -> None:
        """Three invoices a month in January and February, four in March and April.

        The eight March and April invoices stay open and feed the cases. Which invoice plays
        which part is a permutation drawn here, once, and the two that a batch receipt will
        settle are given the same customer, because one receipt comes from one customer."""
        per_month = {1: 3, 2: 3, 3: 4, 4: 4}
        specs = []
        for month, count in per_month.items():
            for _ in range(count):
                specs.append({
                    "customer": self.rng.choice(CUSTOMERS),
                    "issued_on": self.day_in(2026, month, 2, 24),
                    "amount": self.amount(150, 2500),
                })
        open_specs = [sp for sp in specs if sp["issued_on"].month > 2]
        self.open_order = list(range(len(open_specs)))
        self.rng.shuffle(self.open_order)
        batch_a, batch_b = open_specs[self.open_order[2]], open_specs[self.open_order[3]]
        batch_b["customer"] = batch_a["customer"]
        for n, spec in enumerate(specs, start=1):
            inv = {"number": f"INV-{n:04d}", **spec}
            inv["entry_id"] = self.post(
                inv["issued_on"], [("1100", inv["amount"], Decimal(0)), ("4000", Decimal(0), inv["amount"])],
                f"Invoice {inv['number']} to {inv['customer']}", f"invoice:{inv['number']}",
            )
            inv["id"] = self.owner.execute(
                "insert into invoices (company_id, customer_id, number, issued_on, due_on, amount, entry_id)"
                " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                (self.company_id, self.customers[inv["customer"]], inv["number"], inv["issued_on"],
                 inv["issued_on"] + timedelta(days=30), inv["amount"], inv["entry_id"]),
            ).fetchone()[0]
            self.owner.commit()
            self.invoices.append(inv)

    def add_bill(self, vendor: str, received_on: date, amount: Decimal) -> dict:
        n = len(self.bills) + 1
        bill = {"number": f"B-{n:04d}", "vendor": vendor, "received_on": received_on, "amount": amount}
        code = self.vendor_account[vendor]
        bill["entry_id"] = self.post(
            received_on, [(code, amount, Decimal(0)), ("2000", Decimal(0), amount)],
            f"Bill {bill['number']} from {vendor}", f"bill:{bill['number']}",
        )
        bill["id"] = self.owner.execute(
            "insert into bills (company_id, vendor_id, number, received_on, due_on, amount, entry_id)"
            " values (%s, %s, %s, %s, %s, %s, %s) returning id",
            (self.company_id, self.vendors[vendor], bill["number"], received_on,
             received_on + timedelta(days=14), amount, bill["entry_id"]),
        ).fetchone()[0]
        self.owner.commit()
        self.bills.append(bill)
        return bill

    def receive_bills(self) -> None:
        for month in (1, 2, 3, 4):
            for _ in range(2):
                vendor = self.rng.choice([v for v, _ in VENDORS if v != "Amazon"])
                self.add_bill(vendor, self.day_in(2026, month, 2, 24), self.amount(40, 900))

    def settle_history(self) -> None:
        """Everything issued or received in January and February is paid, and recorded."""
        for inv in self.invoices:
            if inv["issued_on"].month > 2:
                continue
            paid_on = inv["issued_on"] + timedelta(days=self.rng.randint(5, 25))
            line_id = self.bank_line(paid_on, inv["amount"], inv["number"], inv["customer"])
            self.post(paid_on, [("1000", inv["amount"], Decimal(0)), ("1100", Decimal(0), inv["amount"])],
                      f"Payment {inv['number']} from {inv['customer']}", f"payment:{inv['number']}", [line_id])
            self.owner.execute("update invoices set paid_amount = amount where id = %s", (inv["id"],))
            self.owner.commit()
        for bill in self.bills:
            if bill["received_on"].month > 2:
                continue
            paid_on = bill["received_on"] + timedelta(days=self.rng.randint(3, 14))
            line_id = self.bank_line(paid_on, -bill["amount"], f"{bill['vendor'].upper()} {bill['number']}", bill["vendor"])
            self.post(paid_on, [("2000", bill["amount"], Decimal(0)), ("1000", Decimal(0), bill["amount"])],
                      f"Payment {bill['number']} to {bill['vendor']}", f"payment:{bill['number']}", [line_id])
            self.owner.execute("update bills set paid_amount = amount where id = %s", (bill["id"],))
            self.owner.commit()

    def recurring_history(self) -> None:
        """Bank fees and a card subscription, booked the way the agent is expected to book them."""
        fee = Decimal("12.00")
        for month in (1, 2, 3):
            last = date(2026, month + 1, 1) - timedelta(days=1)
            line_id = self.bank_line(last, -fee, "MONTHLY ACCOUNT FEE", "")
            self.post(last, [("6200", fee, Decimal(0)), ("1000", Decimal(0), fee)],
                      "Monthly account fee", f"bank-fee:2026-{month:02d}", [line_id])
        for month in (1, 2, 3):
            amount = Decimal("21.00")
            on = date(2026, month, 5)
            line_id = self.bank_line(on, -amount, "GITHUB", "GitHub")
            self.post(on, [("6100", amount, Decimal(0)), ("1000", Decimal(0), amount)],
                      "GitHub subscription", f"github:2026-{month:02d}", [line_id])

    # --- new evidence and ground truth --------------------------------------------------

    def new_evidence(self) -> None:
        """Create the new bank lines and ground truth for the requested labels only.

        Every random draw happens up front, so a one-case task and a whole-month task from
        the same seed see the same amounts and the same invoices. Receipts and bill payments
        are dated from 25 April on, after every invoice and bill of the month has been issued
        (invoices and bills are dated the 2nd to the 24th), so no line pays a document that
        does not exist yet, except the one case built to do exactly that."""
        open_by_issue = [i for i in self.invoices if i["issued_on"].month > 2]
        open_inv = [open_by_issue[k] for k in self.open_order]
        assert len(open_inv) >= 7
        open_bills = [b for b in self.bills if b["received_on"].month > 2]
        late_bill_amount = self.amount(100, 400)

        def receipt(inv, on, ref=None, counterparty=None, amount=None):
            amount = amount or inv["amount"]
            return self.bank_line(on, amount, ref or inv["number"], counterparty or inv["customer"])

        def cash_in(on, amount, credit_code, memo):
            return ExpectedEntry(on, (ExpectedLine("1000", debit=amount), ExpectedLine(credit_code, credit=amount)), memo)

        def cash_out(on, amount, debit_code, memo):
            return ExpectedEntry(on, (ExpectedLine(debit_code, debit=amount), ExpectedLine("1000", credit=amount)), memo)

        def invoice_paid_exactly():
            inv = open_inv[0]
            on = date(2026, 4, 25)
            self.case("invoice_paid_exactly", [receipt(inv, on)],
                      [cash_in(on, inv["amount"], "1100", f"Payment {inv['number']}")])

        def bill_paid_exactly():
            bill = open_bills[0]
            on = date(2026, 4, 25)
            self.case("bill_paid_exactly",
                      [self.bank_line(on, -bill["amount"], f"{bill['vendor'].upper()} {bill['number']}", bill["vendor"])],
                      [cash_out(on, bill["amount"], "2000", f"Payment {bill['number']}")])

        def direct_expense():
            on = date(2026, 4, 5)
            self.case("direct_expense", [self.bank_line(on, Decimal("-21.00"), "GITHUB", "GitHub")],
                      [cash_out(on, Decimal("21.00"), "6100", "GitHub subscription")])

        def bank_fee():
            on = date(2026, 4, 30)
            self.case("bank_fee", [self.bank_line(on, Decimal("-12.00"), "MONTHLY ACCOUNT FEE", "")],
                      [cash_out(on, Decimal("12.00"), "6200", "Monthly account fee")])

        def part_payment():
            inv = open_inv[1]
            part = money(inv["amount"] * Decimal("0.4"))
            on = date(2026, 4, 26)
            self.case("part_payment", [receipt(inv, on, ref=f"{inv['number']} PART", amount=part)],
                      [cash_in(on, part, "1100", f"Part payment {inv['number']}")])

        def batch_payment():
            a, b = open_inv[2], open_inv[3]
            on = date(2026, 4, 27)
            total = a["amount"] + b["amount"]
            self.case("batch_payment", [receipt(a, on, ref=f"{a['number']} {b['number']}", amount=total)],
                      [cash_in(on, total, "1100", f"Payment {a['number']} and {b['number']}")])

        def fee_net_payout():
            a, b = open_inv[4], open_inv[5]
            gross = a["amount"] + b["amount"]
            fees = money(gross * Decimal("0.029") + Decimal("0.60"))
            net = gross - fees
            on = date(2026, 4, 28)
            self.owner.execute(
                "insert into processor_payouts (company_id, processor, paid_on, gross, fees, net, invoice_ids)"
                " values (%s, 'Stripe', %s, %s, %s, %s, %s)", (self.company_id, on, gross, fees, net, [a["id"], b["id"]]),
            )
            self.owner.commit()
            self.case("fee_net_payout", [self.bank_line(on, net, "STRIPE PAYOUT", "Stripe")],
                      [ExpectedEntry(on, (ExpectedLine("1000", debit=net), ExpectedLine("6300", debit=fees),
                                          ExpectedLine("1100", credit=gross)),
                                     f"Stripe payout for {a['number']} and {b['number']}")])

        def intercompany_transfer():
            on = date(2026, 4, 11)
            funding = Decimal("2000.00")
            self.case("intercompany_transfer", [self.bank_line(on, funding, "INTERCO FUNDING", SISTER)],
                      [cash_in(on, funding, "2200", f"Funding from {SISTER}")])

        def bill_after_bank_line():
            paid_on, received_on = date(2026, 4, 3), date(2026, 4, 10)
            late_bill = self.add_bill("Cloudwave Hosting", received_on, late_bill_amount)
            self.case("bill_after_bank_line",
                      [self.bank_line(paid_on, -late_bill["amount"], "CLOUDWAVE HOSTING", "Cloudwave Hosting")],
                      [cash_out(paid_on, late_bill["amount"], "2000", f"Payment {late_bill['number']}")])

        def duplicate_bank_line():
            inv = open_inv[6]
            on = date(2026, 4, 29)
            first = receipt(inv, on)
            second = receipt(inv, on, ref=f"PAYMENT {inv['number'].replace('-', '')}", counterparty=inv["customer"].upper())
            self.case("duplicate_bank_line", [first, second],
                      [cash_in(on, inv["amount"], "1100", f"Payment {inv['number']}")])

        def pending_then_cleared_differently():
            pending = self.bank_line(date(2026, 4, 20), Decimal("-100.00"), "AMZN MKTP", "Amazon", status="pending")
            cleared_on = date(2026, 4, 22)
            cleared = self.bank_line(cleared_on, Decimal("-98.50"), "AMAZON.CO.UK", "Amazon")
            self.case("pending_then_cleared_differently", [pending, cleared],
                      [cash_out(cleared_on, Decimal("98.50"), "6000", "Amazon office supplies")])

        def late_line_in_locked_period():
            late = self.bank_line(date(2026, 2, 27), Decimal("-45.00"), "COURIER EXPRESS", "Courier Express")
            self.case("late_line_in_locked_period", [late],
                      [cash_out(FIRST_OPEN_DAY, Decimal("45.00"), "6000", "Courier Express, bank date 2026-02-27")])

        makers = {f.__name__: f for f in (
            invoice_paid_exactly, bill_paid_exactly, direct_expense, bank_fee, part_payment, batch_payment,
            fee_net_payout, intercompany_transfer, bill_after_bank_line, duplicate_bank_line,
            pending_then_cleared_differently, late_line_in_locked_period,
        )}
        assert set(makers) == set(LABELS)
        for label in LABELS:
            if label in self.labels:
                makers[label]()


def build_world(owner: psycopg.Connection, reviewer: psycopg.Connection, seed: int = 1,
                labels: list[str] | None = None) -> World:
    """Build a world into an empty database. `labels` selects the cases; default is all twelve."""
    return _Builder(owner, reviewer, seed, list(LABELS) if labels is None else list(labels)).build()


if __name__ == "__main__":
    import sys

    from harness import db

    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    with db.connect() as owner, db.connect(db.REVIEWER) as reviewer:
        db.reset_data(owner)
        world = build_world(owner, reviewer, seed)
        print(world.truth_json())
