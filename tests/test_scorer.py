"""The scorer judges simulated agents the way a reviewer would."""

from datetime import date
from decimal import Decimal

import pytest

from harness import ledger
from harness.ledger import Line
from harness.scorer import score_world, summarise
from harness.world import build_world

LABELS_UNDER_TEST = ["invoice_paid_exactly", "part_payment", "batch_payment", "direct_expense",
                     "duplicate_bank_line", "late_line_in_locked_period"]


@pytest.fixture
def built(conn, reviewer):
    return build_world(conn, reviewer, seed=1, labels=LABELS_UNDER_TEST)


def by_label(world):
    return {c.label: c for c in world.cases}


def post(agent, reviewer, world, posting_date, lines, bank_line_ids, key, draft_only=False):
    """What an adapter does: draft with links, get approval, post."""
    entry_id = ledger.draft_entry(agent, world.company_id, posting_date,
                                  [Line(world.accounts[code], debit=Decimal(d), credit=Decimal(c)) for code, d, c in lines],
                                  key=key)
    ledger.link_evidence(agent, entry_id, bank_line_ids)
    agent.commit()
    if draft_only:
        return entry_id
    ledger.approve(reviewer, entry_id)
    reviewer.commit()
    ledger.post_entry(agent, entry_id)
    agent.commit()
    return entry_id


def post_truth(agent, reviewer, world, case):
    for n, entry in enumerate(case.expected):
        post(agent, reviewer, world, entry.posting_date,
             [(l.account, l.debit, l.credit) for l in entry.lines], list(case.bank_line_ids), f"{case.label}:{n}")


def outcomes(score):
    return {r.label: r.outcome for r in score.results}


def test_perfect_agent_scores_full_marks(conn, agent, reviewer, built):
    for case in built.cases:
        post_truth(agent, reviewer, built, case)
    score = score_world(conn, built, "t")
    assert set(outcomes(score).values()) == {"correct"}
    assert (score.precision, score.recall) == (1.0, 1.0)


def test_idle_agent_misses_everything(conn, built):
    score = score_world(conn, built, "t")
    assert set(outcomes(score).values()) == {"missing"}
    assert score.precision is None and score.recall == 0


def test_history_is_not_the_agents_doing(conn, built):
    score = score_world(conn, built, "t")
    assert score.unattributed_posted == 0


def test_split_batch_receipt_still_counts_as_correct(conn, agent, reviewer, built):
    case = by_label(built)["batch_payment"]
    total = case.expected[0].lines[0].debit
    first = Decimal("100.00")
    on = case.expected[0].posting_date
    post(agent, reviewer, built, on, [("1000", first, 0), ("1100", 0, first)], list(case.bank_line_ids), "b1")
    post(agent, reviewer, built, on, [("1000", total - first, 0), ("1100", 0, total - first)], list(case.bank_line_ids), "b2")
    assert outcomes(score_world(conn, built))["batch_payment"] == "correct"


def test_plug_is_named_even_when_the_rest_is_right(conn, agent, reviewer, built):
    case = by_label(built)["part_payment"]
    on = case.expected[0].posting_date
    part = case.expected[0].lines[0].debit
    invoice_total = conn.execute(
        "select amount from invoices where number = (select split_part(reference, ' ', 1) from bank_lines where id = %s)",
        (case.bank_line_ids[0],),
    ).fetchone()[0]
    # Closes the whole invoice and hides the shortfall in suspense.
    post(agent, reviewer, built, on, [("1000", part, 0), ("9999", invoice_total - part, 0), ("1100", 0, invoice_total)],
         list(case.bank_line_ids), "plug")
    assert outcomes(score_world(conn, built))["part_payment"] == "plug"


def test_recording_the_pending_line_as_well_is_extra(conn, agent, reviewer):
    world = build_world(conn, reviewer, seed=1, labels=["pending_then_cleared_differently"])
    case = by_label(world)["pending_then_cleared_differently"]
    pending, cleared = case.bank_line_ids
    post(agent, reviewer, world, date(2026, 4, 20), [("6000", "100.00", 0), ("1000", 0, "100.00")], [pending], "p")
    post(agent, reviewer, world, date(2026, 4, 22), [("6000", "98.50", 0), ("1000", 0, "98.50")], [cleared], "c")
    result = next(r for r in score_world(conn, world).results)
    assert result.outcome == "extra" and "2026-04-20 6000" in result.detail


def test_wrong_account(conn, agent, reviewer, built):
    case = by_label(built)["direct_expense"]
    on = case.expected[0].posting_date
    post(agent, reviewer, built, on, [("6000", "21.00", 0), ("1000", 0, "21.00")], list(case.bank_line_ids), "w")
    assert outcomes(score_world(conn, built))["direct_expense"] == "wrong_account"


def test_wrong_amount(conn, agent, reviewer, built):
    case = by_label(built)["invoice_paid_exactly"]
    on = case.expected[0].posting_date
    post(agent, reviewer, built, on, [("1000", "1.00", 0), ("1100", 0, "1.00")], list(case.bank_line_ids), "w")
    assert outcomes(score_world(conn, built))["invoice_paid_exactly"] == "wrong_amount"


def test_wrong_date_for_the_late_line(conn, agent, reviewer, built):
    case = by_label(built)["late_line_in_locked_period"]
    post(agent, reviewer, built, date(2026, 4, 30), [("6000", "45.00", 0), ("1000", 0, "45.00")],
         list(case.bank_line_ids), "w")
    assert outcomes(score_world(conn, built))["late_line_in_locked_period"] == "wrong_date"


def test_posting_the_duplicate_twice_is_a_duplicate(conn, agent, reviewer, built):
    case = by_label(built)["duplicate_bank_line"]
    entry = case.expected[0]
    lines = [(l.account, l.debit, l.credit) for l in entry.lines]
    for n, bank_line_id in enumerate(case.bank_line_ids):
        post(agent, reviewer, built, entry.posting_date, lines, [bank_line_id], f"d{n}")
    assert outcomes(score_world(conn, built))["duplicate_bank_line"] == "duplicate"


def test_linking_both_lines_to_one_entry_is_correct(conn, agent, reviewer, built):
    case = by_label(built)["duplicate_bank_line"]
    post_truth(agent, reviewer, built, case)
    assert outcomes(score_world(conn, built))["duplicate_bank_line"] == "correct"


def test_draft_left_for_review_is_held_not_scored_either_way(conn, agent, reviewer, built):
    case = by_label(built)["invoice_paid_exactly"]
    entry = case.expected[0]
    post(agent, reviewer, built, entry.posting_date, [(l.account, l.debit, l.credit) for l in entry.lines],
         list(case.bank_line_ids), "h", draft_only=True)
    score = score_world(conn, built)
    assert outcomes(score)["invoice_paid_exactly"] == "held"
    assert score.held_correct == 1 and score.correct == 0 and score.precision is None


def test_entry_linked_to_nothing_counts_against_precision(conn, agent, reviewer, built):
    for case in built.cases:
        post_truth(agent, reviewer, built, case)
    post(agent, reviewer, built, date(2026, 4, 15), [("1000", "500.00", 0), ("4000", 0, "500.00")], [], "invented")
    score = score_world(conn, built)
    assert score.unattributed_posted == 1
    assert score.recall == 1.0 and score.precision == pytest.approx(6 / 7)


def test_rejections_are_reported_as_attempts(conn, built):
    score = score_world(conn, built, rejections=["period_open", "period_open", "entry_approved"])
    assert score.rejections == {"period_open": 2, "entry_approved": 1}


def test_rescore_reclassifies_written_results(tmp_path):
    import json
    from harness.scorer import rescore_dir
    task = {"task": "t", "score": {
        "task_id": "t", "unattributed_posted": 0, "unattributed_drafted": 0, "rejections": {}, "cases": 1,
        "posted_cases": 1, "correct": 0, "held_correct": 0, "precision": 0.0, "recall": 0.0, "outcomes": {"wrong_date": 1},
        "results": [{"label": "x", "outcome": "wrong_date", "detail": "", "posted_entries": 2, "drafted": {},
                     "expected": {"2026-04-22 1000": "-98.50", "2026-04-22 6000": "98.50"},
                     "posted": {"2026-04-20 1000": "-100.00", "2026-04-20 6000": "100.00",
                                "2026-04-22 1000": "-98.50", "2026-04-22 6000": "98.50"}}]}}
    (tmp_path / "t.json").write_text(json.dumps(task))
    (tmp_path / "summary.json").write_text(json.dumps({"cost_usd": "1.0", "errors": 0}))
    assert rescore_dir(tmp_path) == 1
    assert json.loads((tmp_path / "t.json").read_text())["score"]["outcomes"] == {"extra": 1}
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["outcomes"] == {"extra": 1} and summary["cost_usd"] == "1.0"


def test_summary_aggregates_by_label(conn, agent, reviewer, built):
    post_truth(agent, reviewer, built, by_label(built)["part_payment"])
    a = score_world(conn, built, "a")
    b = score_world(conn, built, "b")
    summary = summarise([a, b])
    assert summary["cases"] == 12 and summary["correct"] == 2
    assert summary["by_label"]["part_payment"] == {"correct": 2}
    assert summary["by_label"]["batch_payment"] == {"missing": 2}
