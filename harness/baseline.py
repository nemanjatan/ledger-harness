"""A scripted, deterministic bookkeeper. It sees the world only through the toolbox.

Rules, in the order tried for each unrecorded bank line:
  1. Pending lines are left alone; the cleared line will come.
  2. A line with the same date, amount and counterparty as one already handled in this run
     is a duplicate feed line: it is linked to that entry and nothing more is posted.
  3. Money in: invoice numbers in the reference, else a payout report with that net amount,
     else the counterparty's open invoices if one matches the amount exactly. Settles
     receivables; a shortfall against a single invoice is a part payment.
  4. Money out: a bill number in the reference, else an open bill from that vendor for that
     amount, else the vendor's default expense account, else the account history used for the
     same reference.
  5. Anything else is flagged for a human. Nothing is ever posted to suspense.
Posting date is the bank date, or the first open day if that period is locked.

It encodes the rules the task set was designed around, so it should do well. It is a
calibration of the harness, not a competitor: it has no rule for intercompany funding, so it
flags that line, which is what a rule-based system does with what it cannot match.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from harness.tools import Toolbox

INVOICE_RE = re.compile(r"INV-?(\d{4})", re.IGNORECASE)
BILL_RE = re.compile(r"\bB-?(\d{4})\b", re.IGNORECASE)

CASH, RECEIVABLES, PAYABLES, FEES = "1000", "1100", "2000", "6300"


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class BaselineAgent:
    name = "baseline"

    model = "scripted"

    def run(self, tools: Toolbox, instructions: str = "") -> None:
        periods = tools.list_periods()
        open_days = [date.fromisoformat(p["starts_on"]) for p in periods if p["status"] == "open"]
        first_open = min(open_days) if open_days else None
        locked = [(date.fromisoformat(p["starts_on"]), date.fromisoformat(p["ends_on"]))
                  for p in periods if p["status"] == "locked"]
        invoices = tools.open_invoices()
        bills = tools.open_bills()
        vendors = {norm(v["name"]): v for v in tools.list_vendors()}
        payouts = tools.payout_reports()
        handled: dict[tuple, int] = {}      # (date, amount, normalised counterparty) -> entry id

        for line in tools.unrecorded_bank_lines():
            if line["status"] == "pending":
                continue
            amount = Decimal(line["amount"])
            booked_on = date.fromisoformat(line["booked_on"])
            signature = (line["booked_on"], line["amount"], norm(line["counterparty"]))
            if signature in handled:
                self.link_duplicate(tools, handled[signature], line["id"])
                continue

            posting_date = booked_on
            memo_suffix = ""
            if any(s <= booked_on <= e for s, e in locked) and first_open:
                posting_date = first_open
                memo_suffix = f", bank date {booked_on.isoformat()}"

            lines, memo, rule = (self.money_in if amount > 0 else self.money_out)(
                tools, line, abs(amount), invoices, bills, vendors, payouts)
            if lines is None:
                tools.flag([line["id"]], memo)
                continue
            result = tools.draft_entry(posting_date.isoformat(), memo + memo_suffix, lines, [line["id"]],
                                       idempotency_key=f"bank-line:{line['id']}")
            if not result["ok"]:
                tools.flag([line["id"]], f"draft refused: {result['error']}")
                continue
            posted = tools.post_entry(result["entry_id"], {
                "evidence": [f"bank line {line['id']}"], "rule": rule, "confidence": 1.0,
                "reason": f"Scripted rule '{rule}' matched the line {line['reference']!r} from {line['counterparty']!r}.",
            })
            if posted["ok"]:
                handled[signature] = result["entry_id"]
            else:
                tools.flag([line["id"]], f"post refused: {posted['error']}", entry_id=result["entry_id"])

    @staticmethod
    def link_duplicate(tools: Toolbox, entry_id: int, bank_line_id: int) -> None:
        # The covering entry is posted and frozen, so the duplicate is recorded as a flag with
        # the covering entry named. The scorer sees no effect for the line, which is right.
        tools.flag([bank_line_id], f"duplicate of a line recorded by entry {entry_id}", entry_id=entry_id)

    def money_in(self, tools, line, amount, invoices, bills, vendors, payouts):
        numbers = {f"INV-{n}" for n in INVOICE_RE.findall(line["reference"])}
        if numbers:
            matched = [i for i in invoices if i["number"] in numbers]
            outstanding = sum(Decimal(i["outstanding"]) for i in matched)
            if matched and outstanding == amount:
                self.settle(invoices, matched, amount)
                return self.cash_in(amount, RECEIVABLES), "Payment " + " ".join(sorted(numbers)), "invoice numbers in reference, amounts agree"
            if len(matched) == 1 and amount < outstanding:
                self.settle(invoices, matched, amount)
                return self.cash_in(amount, RECEIVABLES), f"Part payment {matched[0]['number']}", "single invoice in reference, amount below outstanding"
            return None, f"reference names {sorted(numbers)} but amounts do not agree", None
        if "PAYOUT" in line["reference"].upper():
            for p in payouts:
                if Decimal(p["net"]) == amount:
                    matched = [i for i in invoices if i["number"] in p["invoices"]]
                    self.settle(invoices, matched, Decimal(p["gross"]))
                    return ([{"account": CASH, "debit": p["net"]}, {"account": FEES, "debit": p["fees"]},
                             {"account": RECEIVABLES, "credit": p["gross"]}],
                            f"{p['processor']} payout for {' '.join(p['invoices'])}", "payout report net equals receipt")
            return None, "payout with no matching report", None
        theirs = [i for i in invoices if norm(i["customer"]) == norm(line["counterparty"])]
        exact = [i for i in theirs if Decimal(i["outstanding"]) == amount]
        if len(exact) == 1:
            self.settle(invoices, exact, amount)
            return self.cash_in(amount, RECEIVABLES), f"Payment {exact[0]['number']}", "counterparty has one open invoice for this amount"
        return None, "receipt matches no invoice or payout report", None

    def money_out(self, tools, line, amount, invoices, bills, vendors, payouts):
        numbers = {f"B-{n}" for n in BILL_RE.findall(line["reference"])}
        matched = [b for b in bills if b["number"] in numbers]
        if matched and sum(Decimal(b["outstanding"]) for b in matched) == amount:
            self.settle(bills, matched, amount)
            return self.cash_out(amount, PAYABLES), "Payment " + " ".join(sorted(numbers)), "bill number in reference, amount agrees"
        vendor = vendors.get(norm(line["counterparty"]))
        if vendor:
            theirs = [b for b in bills if norm(b["vendor"]) == norm(vendor["name"]) and Decimal(b["outstanding"]) == amount]
            if len(theirs) == 1:
                self.settle(bills, theirs, amount)
                return self.cash_out(amount, PAYABLES), f"Payment {theirs[0]['number']}", "vendor has one open bill for this amount"
            if vendor["default_expense_account"]:
                return self.cash_out(amount, vendor["default_expense_account"]), f"{vendor['name']} {line['reference']}".strip(), "vendor default expense account"
        for past in tools.history_for(line["reference"], limit=1):
            debit_codes = [l["account"] for l in past["lines"] if Decimal(l["debit"]) > 0 and l["account"] != CASH]
            if len(debit_codes) == 1:
                return self.cash_out(amount, debit_codes[0]), past["memo"], "same reference booked before"
        return None, "payment matches no bill, vendor or precedent", None

    @staticmethod
    def settle(documents, matched, amount):
        """Keep the in-memory outstanding figures honest within one run."""
        remaining = amount
        for doc in matched:
            take = min(Decimal(doc["outstanding"]), remaining)
            doc["outstanding"] = str(Decimal(doc["outstanding"]) - take)
            remaining -= take

    @staticmethod
    def cash_in(amount, credit_code):
        return [{"account": CASH, "debit": str(amount)}, {"account": credit_code, "credit": str(amount)}]

    @staticmethod
    def cash_out(amount, debit_code):
        return [{"account": debit_code, "debit": str(amount)}, {"account": CASH, "credit": str(amount)}]
