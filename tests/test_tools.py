"""The toolbox: validation is the database's verdict, refusals are recorded, nothing leaks."""

from datetime import date

import pytest

from harness.tools import Toolbox
from harness.world import build_world

LABELS = ["invoice_paid_exactly", "late_line_in_locked_period", "duplicate_bank_line"]


@pytest.fixture
def setup(conn, agent, reviewer):
    world = build_world(conn, reviewer, seed=1, labels=LABELS)
    return world, Toolbox(agent, reviewer, world.company_id)


TRACE = {"evidence": ["bank line 1"], "rule": "test", "confidence": 0.9, "reason": "because"}


def cash_sale(amount="10.00", credit="4000"):
    return [{"account": "1000", "debit": amount}, {"account": credit, "credit": amount}]


def test_read_tools_see_evidence_not_ground_truth(setup):
    world, tools = setup
    unrecorded = tools.unrecorded_bank_lines()
    assert {l["id"] for l in unrecorded} == world.new_bank_line_ids()
    assert all(isinstance(l["amount"], str) for l in unrecorded)
    assert any(i["outstanding"] == i["amount"] for i in tools.open_invoices())
    assert [p["status"] for p in tools.list_periods()] == ["locked", "locked", "open", "open"]
    assert "is_suspense" not in tools.list_accounts()[0]
    fees = tools.history_for("MONTHLY ACCOUNT FEE", limit=2)
    assert len(fees) == 2 and {l["account"] for l in fees[0]["lines"]} == {"1000", "6200"}


def test_validate_returns_the_invariant_name_and_keeps_nothing(setup):
    world, tools = setup
    bad = [{"account": "1000", "debit": "10.00"}, {"account": "4000", "credit": "9.00"}]
    result = tools.validate_entry("2026-04-02", "x", bad)
    assert result == {"ok": False, "error": "entry_balanced", "message": result["message"]}
    good = tools.validate_entry("2026-04-02", "x", cash_sale())
    assert good == {"ok": True}
    assert tools.agent.execute("select count(*) from journal_entries where id > %s", (world.history_max_entry_id,)).fetchone()[0] == 0
    assert [c.tool for c in tools.calls][-2:] == ["validate_entry", "validate_entry"]


def test_draft_refused_for_unknown_account_or_bad_line(setup):
    world, tools = setup
    assert tools.draft_entry("2026-04-02", "x", [{"account": "1000", "debit": "1"}, {"account": "0000", "credit": "1"}])["error"] == "bad_request"
    assert tools.draft_entry("2026-04-02", "x", [{"account": "1000", "debit": "1", "credit": "1"}, {"account": "4000", "credit": "1"}])["error"] == "line_is_debit_xor_credit"


def test_draft_then_post_goes_through_the_reviewer(setup):
    world, tools = setup
    line_id = next(iter(world.new_bank_line_ids()))
    drafted = tools.draft_entry("2026-04-02", "sale", cash_sale(), [line_id], idempotency_key="k1")
    assert drafted["ok"]
    entry = tools.get_entry(drafted["entry_id"])
    assert entry["status"] == "draft" and entry["bank_line_ids"] == [line_id]
    posted = tools.post_entry(drafted["entry_id"], TRACE)
    assert posted == {"ok": True, "entry_id": drafted["entry_id"], "status": "posted"}
    approved_by = tools.agent.execute("select approved_by from approvals where entry_id = %s", (drafted["entry_id"],)).fetchone()[0]
    assert approved_by == "ledger_reviewer"
    assert tools.post_entry(drafted["entry_id"], TRACE)["ok"], "posting again is a no-op"
    assert tools.refusals == []


def test_posting_into_a_locked_period_is_refused_and_recorded(setup):
    world, tools = setup
    drafted = tools.draft_entry("2026-02-27", "late", cash_sale())
    assert drafted["ok"], "a draft may sit in a locked period"
    posted = tools.post_entry(drafted["entry_id"], TRACE)
    assert posted["ok"] is False and posted["error"] == "period_open"
    assert tools.refusals == ["period_open"]
    assert tools.get_entry(drafted["entry_id"])["status"] == "draft"


def test_same_idempotency_key_does_not_make_a_second_draft(setup):
    world, tools = setup
    assert tools.draft_entry("2026-04-02", "x", cash_sale(), idempotency_key="once")["ok"]
    again = tools.draft_entry("2026-04-02", "x", cash_sale(), idempotency_key="once")
    assert again["ok"] is False and "idempotency" in again["error"]


def test_discard_draft_even_after_approval_but_never_after_posting(setup):
    world, tools = setup
    drafted = tools.draft_entry("2026-04-02", "x", cash_sale())
    tools.post_entry(drafted["entry_id"], TRACE)
    assert tools.discard_draft(drafted["entry_id"])["error"] == "posted_entry_immutable"
    other = tools.draft_entry("2026-02-27", "late", cash_sale())
    tools.post_entry(other["entry_id"], TRACE)                     # approved by the reviewer, refused by the period
    assert tools.discard_draft(other["entry_id"]) == {"ok": True}
    assert tools.get_entry(other["entry_id"]) is None


def test_flag_is_recorded_for_the_scorer(setup):
    world, tools = setup
    line_id = next(iter(world.new_bank_line_ids()))
    tools.flag([line_id], "not sure")
    assert tools.flags == [{"bank_line_ids": [line_id], "reason": "not sure", "entry_id": None}]


def test_no_trace_no_post(setup):
    world, tools = setup
    drafted = tools.draft_entry("2026-04-02", "x", cash_sale())
    bad = tools.post_entry(drafted["entry_id"], {"evidence": [], "rule": "", "confidence": 2, "reason": "r"})
    assert bad["ok"] is False and bad["error"] == "invalid_reasoning"
    assert "evidence" in bad["message"] and "confidence" in bad["message"]
    assert tools.get_entry(drafted["entry_id"])["status"] == "draft" and tools.traces == []
    good = tools.post_entry(drafted["entry_id"], TRACE)
    assert good["ok"] and tools.traces == [{"entry_id": drafted["entry_id"], "posted": True, "reasoning": TRACE}]


def test_history_for_matches_literally(setup):
    world, tools = setup
    assert tools.history_for("%") == []
    assert tools.history_for("_") == []
    assert len(tools.history_for("MONTHLY", limit=10)) == 3


def test_discard_draft_stays_inside_the_company(conn, setup):
    from decimal import Decimal
    from harness.ledger import Line, draft_entry
    world, tools = setup
    other_id = conn.execute("select id from companies where name = 'Beta Ltd'").fetchone()[0]
    cash = conn.execute("insert into accounts (company_id, code, name, kind) values (%s, '1000', 'Cash', 'asset') returning id", (other_id,)).fetchone()[0]
    sales = conn.execute("insert into accounts (company_id, code, name, kind) values (%s, '4000', 'Sales', 'revenue') returning id", (other_id,)).fetchone()[0]
    foreign = draft_entry(conn, other_id, world.today, [Line(cash, debit=Decimal("5.00")), Line(sales, credit=Decimal("5.00"))])
    conn.commit()
    assert tools.discard_draft(foreign) == {"ok": False, "error": "no_such_draft"}
    assert conn.execute("select count(*) from journal_lines where entry_id = %s", (foreign,)).fetchone()[0] == 2
