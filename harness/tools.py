"""The agent interface: what any agent, scripted or model, sees and can do.

Read tools return plain data (strings for money and dates) so the same methods can back an
LLM tool schema or an MCP server without translation. Write tools mirror the draft, validate,
post pattern. Validation is the database's own opinion: the entry is inserted, deferred
checks are forced with SET CONSTRAINTS ALL IMMEDIATE, the constraint name is read, and the
transaction is rolled back. Enforcement and validation cannot drift because they are the
same code.

The toolbox holds two connections. The agent's own (role ledger_agent) does everything the
agent asks. The reviewer's (role ledger_reviewer) stands in for the human: it approves any
draft the agent asks to post. That is deliberate. The harness measures what an agent would
put into the books if the human said yes, which is the number that matters when deciding
what to let it post unattended; the approval step is real in the database so that an agent
cannot skip it, not a judgement the harness pretends to make.

Every call is logged with its arguments and result, and every refusal the database gives on
posting is recorded by invariant name for the scorer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg
from jsonschema import Draft202012Validator
from psycopg.errors import CheckViolation, ForeignKeyViolation, NotNullViolation, UniqueViolation

from harness import ledger
from harness.ledger import Line


TRACE_SCHEMA = json.loads(Path(__file__).with_name("trace_schema.json").read_text())
_trace_validator = Draft202012Validator(TRACE_SCHEMA)


def trace_errors(reasoning) -> list[str]:
    """Why a reasoning trace does not meet the schema; empty when it does."""
    return [f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
            for e in sorted(_trace_validator.iter_errors(reasoning), key=lambda e: list(e.path))]


def plain(value):
    """Decimals and dates as strings, recursively, so results are JSON-ready."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


@dataclass
class Call:
    tool: str
    args: dict
    result: dict | list


@dataclass
class Toolbox:
    agent: psycopg.Connection
    reviewer: psycopg.Connection
    company_id: int
    calls: list[Call] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.accounts = {
            code: aid for aid, code in self.agent.execute(
                "select id, code from accounts where company_id = %s", (self.company_id,)
            ).fetchall()
        }
        self.agent.commit()

    # --- plumbing ---------------------------------------------------------------------

    def _rows(self, sql: str, params=()) -> list[dict]:
        with self.agent.cursor(row_factory=psycopg.rows.dict_row) as cur:
            rows = cur.execute(sql, params).fetchall()
        self.agent.commit()
        return plain(rows)

    def _record(self, tool: str, args: dict, result):
        self.calls.append(Call(tool, plain(args), result))
        return result

    # --- read tools -------------------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        return self._record("list_accounts", {}, self._rows(
            "select code, name, kind from accounts where company_id = %s order by code", (self.company_id,)))

    def list_periods(self) -> list[dict]:
        return self._record("list_periods", {}, self._rows(
            "select starts_on, ends_on, status from periods where company_id = %s order by starts_on",
            (self.company_id,)))

    def unrecorded_bank_lines(self) -> list[dict]:
        return self._record("unrecorded_bank_lines", {}, self._rows(
            "select b.id, b.booked_on, b.amount, b.reference, b.counterparty, b.status from bank_lines b"
            " where b.company_id = %s and not exists (select 1 from entry_evidence v where v.bank_line_id = b.id)"
            " order by b.booked_on, b.id", (self.company_id,)))

    def open_invoices(self) -> list[dict]:
        return self._record("open_invoices", {}, self._rows(
            "select i.id, i.number, c.name as customer, i.issued_on, i.due_on, i.amount,"
            " i.amount - i.paid_amount as outstanding from invoices i join customers c on c.id = i.customer_id"
            " where i.company_id = %s and i.paid_amount < i.amount order by i.issued_on", (self.company_id,)))

    def open_bills(self) -> list[dict]:
        return self._record("open_bills", {}, self._rows(
            "select b.id, b.number, v.name as vendor, b.received_on, b.due_on, b.amount,"
            " b.amount - b.paid_amount as outstanding from bills b join vendors v on v.id = b.vendor_id"
            " where b.company_id = %s and b.paid_amount < b.amount order by b.received_on", (self.company_id,)))

    def list_vendors(self) -> list[dict]:
        return self._record("list_vendors", {}, self._rows(
            "select v.name, a.code as default_expense_account from vendors v"
            " left join accounts a on a.id = v.expense_account_id where v.company_id = %s order by v.name",
            (self.company_id,)))

    def payout_reports(self) -> list[dict]:
        return self._record("payout_reports", {}, self._rows(
            "select p.id, p.processor, p.paid_on, p.gross, p.fees, p.net,"
            " array(select number from invoices i where i.id = any(p.invoice_ids) order by number) as invoices"
            " from processor_payouts p where p.company_id = %s order by p.paid_on", (self.company_id,)))

    def history_for(self, reference_like: str, limit: int = 5) -> list[dict]:
        """Recorded bank lines whose reference or counterparty contains the text, with the entry that recorded them.

        The text is matched literally: LIKE wildcards in it are escaped, so "%" finds a
        percent sign, not everything."""
        escaped = str(reference_like).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        return self._record("history_for", {"reference_like": reference_like, "limit": limit}, self._rows(
            "select b.id as bank_line_id, b.booked_on, b.amount, b.reference, b.counterparty,"
            " e.id as entry_id, e.posting_date, e.memo,"
            " (select json_agg(json_build_object('account', a.code, 'debit', l.debit, 'credit', l.credit) order by l.id)"
            "    from journal_lines l join accounts a on a.id = l.account_id where l.entry_id = e.id) as lines"
            " from bank_lines b join entry_evidence v on v.bank_line_id = b.id"
            " join journal_entries e on e.id = v.entry_id"
            " where b.company_id = %s and (b.reference ilike %s or b.counterparty ilike %s) and e.status = 'posted'"
            " order by b.booked_on desc limit %s", (self.company_id, pattern, pattern, limit)))

    def get_entry(self, entry_id: int) -> dict | None:
        rows = self._rows(
            "select e.id, e.posting_date, e.memo, e.status,"
            " (select json_agg(json_build_object('account', a.code, 'debit', l.debit, 'credit', l.credit) order by l.id)"
            "    from journal_lines l join accounts a on a.id = l.account_id where l.entry_id = e.id) as lines,"
            " array(select bank_line_id from entry_evidence v where v.entry_id = e.id order by 1) as bank_line_ids"
            " from journal_entries e where e.id = %s and e.company_id = %s", (entry_id, self.company_id))
        return self._record("get_entry", {"entry_id": entry_id}, rows[0] if rows else None)

    # --- write tools ------------------------------------------------------------------

    def _insert(self, posting_date, memo, lines, bank_line_ids, idempotency_key) -> int:
        parsed = []
        for ln in lines:
            code = str(ln.get("account", ""))
            if code not in self.accounts:
                raise ValueError(f"unknown account code {code!r}")
            try:
                debit = Decimal(str(ln.get("debit", "0")))
                credit = Decimal(str(ln.get("credit", "0")))
            except InvalidOperation as exc:
                raise ValueError(f"bad amount in line {ln}") from exc
            parsed.append(Line(self.accounts[code], debit=debit, credit=credit, description=str(ln.get("description", ""))))
        entry_id = ledger.draft_entry(self.agent, self.company_id, date.fromisoformat(str(posting_date)), parsed,
                                      memo=str(memo), key=idempotency_key)
        ledger.link_evidence(self.agent, entry_id, [int(b) for b in bank_line_ids])
        self.agent.execute("set constraints all immediate")   # deferred checks fire now, not at commit
        return entry_id

    def _attempt(self, tool: str, args: dict, keep: bool) -> dict:
        try:
            entry_id = self._insert(**args)
        except CheckViolation as exc:
            self.agent.rollback()
            result = {"ok": False, "error": exc.diag.constraint_name, "message": exc.diag.message_primary}
        except (UniqueViolation, ForeignKeyViolation, NotNullViolation) as exc:
            self.agent.rollback()
            result = {"ok": False, "error": exc.diag.constraint_name or type(exc).__name__, "message": exc.diag.message_primary}
        except ValueError as exc:
            self.agent.rollback()
            result = {"ok": False, "error": "bad_request", "message": str(exc)}
        else:
            if keep:
                self.agent.commit()
                result = {"ok": True, "entry_id": entry_id}
            else:
                self.agent.rollback()
                result = {"ok": True}
        return self._record(tool, args, result)

    def validate_entry(self, posting_date, memo, lines, bank_line_ids=(), idempotency_key=None) -> dict:
        """Dry run: the database's verdict on the entry, nothing kept."""
        return self._attempt("validate_entry", dict(posting_date=posting_date, memo=memo, lines=lines,
                                                    bank_line_ids=list(bank_line_ids), idempotency_key=idempotency_key), keep=False)

    def draft_entry(self, posting_date, memo, lines, bank_line_ids=(), idempotency_key=None) -> dict:
        """Create a draft. Refused outright if it would break an invariant."""
        return self._attempt("draft_entry", dict(posting_date=posting_date, memo=memo, lines=lines,
                                                 bank_line_ids=list(bank_line_ids), idempotency_key=idempotency_key), keep=True)

    def post_entry(self, entry_id: int, reasoning: dict) -> dict:
        """Ask for the draft to be approved and posted, with a reasoning trace.

        No trace, no post: the trace is validated against trace_schema.json first. Refusals by
        the database are recorded for the scorer."""
        args = {"entry_id": entry_id, "reasoning": reasoning}
        problems = trace_errors(reasoning)
        if problems:
            return self._record("post_entry", args,
                                {"ok": False, "error": "invalid_reasoning", "message": "; ".join(problems)})
        try:
            ledger.approve(self.reviewer, entry_id, note="harness reviewer")
            self.reviewer.commit()
        except UniqueViolation:
            self.reviewer.rollback()                       # already approved
        except (CheckViolation, ForeignKeyViolation) as exc:
            self.reviewer.rollback()
            if exc.diag.constraint_name != "approval_before_posting":
                return self._record("post_entry", args,
                                    {"ok": False, "error": exc.diag.constraint_name or "no_such_entry", "message": exc.diag.message_primary})
        try:
            ledger.post_entry(self.agent, entry_id)
            self.agent.commit()
            result = {"ok": True, "entry_id": entry_id, "status": "posted"}
        except CheckViolation as exc:
            self.agent.rollback()
            self.refusals.append(exc.diag.constraint_name)
            result = {"ok": False, "error": exc.diag.constraint_name, "message": exc.diag.message_primary}
        except psycopg.errors.NoDataFound as exc:
            self.agent.rollback()
            result = {"ok": False, "error": "no_such_entry", "message": exc.diag.message_primary}
        self.traces.append({"entry_id": entry_id, "posted": result["ok"], "reasoning": reasoning})
        return self._record("post_entry", args, result)

    def discard_draft(self, entry_id: int) -> dict:
        """Delete a draft the agent no longer wants. The reviewer withdraws any approval first."""
        try:
            ledger.withdraw_approval(self.reviewer, entry_id)
            self.reviewer.commit()
            self.agent.execute("delete from entry_evidence where entry_id = %s and company_id = %s", (entry_id, self.company_id))
            self.agent.execute("delete from journal_lines where entry_id = %s and company_id = %s", (entry_id, self.company_id))
            deleted = self.agent.execute(
                "delete from journal_entries where id = %s and company_id = %s", (entry_id, self.company_id)).rowcount
            self.agent.commit()
            result = {"ok": deleted == 1} if deleted == 1 else {"ok": False, "error": "no_such_draft"}
        except CheckViolation as exc:
            self.reviewer.rollback()
            self.agent.rollback()
            result = {"ok": False, "error": exc.diag.constraint_name, "message": exc.diag.message_primary}
        return self._record("discard_draft", {"entry_id": entry_id}, result)

    def flag(self, bank_line_ids: list[int], reason: str, entry_id: int | None = None) -> dict:
        """Leave something for a human: a line the agent will not record, or a draft it will not post."""
        item = {"bank_line_ids": [int(b) for b in bank_line_ids], "reason": str(reason), "entry_id": entry_id}
        self.flags.append(item)
        return self._record("flag", item, {"ok": True})

    # --- for the trace ----------------------------------------------------------------

    def calls_json(self) -> str:
        return json.dumps([{"tool": c.tool, "args": c.args, "result": c.result} for c in self.calls], indent=2)
