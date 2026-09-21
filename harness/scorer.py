"""The scorer: a state diff between what the ground truth expects and what is in the ledger.

Unit of judgement: the case. An agent's entries (every entry created after the world was
built) are attributed to cases through the bank lines they link. For each case the expected
effect and the actual effect are compared as {(posting date, account code): net debit}. That
makes the comparison indifferent to how the agent split or worded its entries: one batch
receipt or two, any memo, and it still passes if the ledger ends up in the same state.

Outcomes, one per case, in the order they are decided:
  plug           a posted entry for the case touches a suspense-flagged account
  duplicate      the posted effect is the expected effect two or more times over
  correct        posted effect equals expected effect
  extra          the expected effect was posted, and something more with it (for example the
                 pending line recorded as well as the cleared one)
  missing        nothing posted, nothing drafted, nothing said
  flagged        nothing posted or drafted, but the agent flagged a line of the case for a human
  held           nothing posted; a draft exists (its own correctness is reported alongside)
  wrong_account  a different set of accounts was hit
  wrong_date     same accounts, different posting dates
  wrong_amount   same accounts and dates, different amounts

Two things the database refuses can therefore never appear as outcomes: posting into a locked
period and posting without approval. The adapter records every refusal it receives, and the
scorer reports those as attempts, by invariant name.

Precision is over what the agent posted: correct cases divided by posted cases plus posted
entries attributed to no case. Recall is correct cases divided by all cases. Held drafts count
for neither; they are reported on their own line, because "draft and wait for a human" is a
legitimate answer that a precision figure should not punish and a recall figure should not
reward.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal

import psycopg

from harness.world import World

Effect = dict[tuple[str, str], Decimal]     # (posting date iso, account code) -> net debit


@dataclass
class CaseResult:
    label: str
    outcome: str
    expected: dict[str, str]
    posted: dict[str, str]
    drafted: dict[str, str]
    posted_entries: int
    detail: str = ""


@dataclass
class Score:
    task_id: str
    results: list[CaseResult]
    unattributed_posted: int
    unattributed_drafted: int
    rejections: dict[str, int]
    cases: int = 0
    posted_cases: int = 0
    correct: int = 0
    held_correct: int = 0
    precision: float | None = None
    recall: float = 0.0

    def outcomes(self) -> Counter:
        return Counter(r.outcome for r in self.results)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["outcomes"] = dict(self.outcomes())
        return d


def _effect_of_expected(case) -> Effect:
    effect: Effect = defaultdict(Decimal)
    for entry in case.expected:
        for line in entry.lines:
            effect[(entry.posting_date.isoformat(), line.account)] += line.debit - line.credit
    return {k: v for k, v in effect.items() if v != 0}


def _render(effect: Effect) -> dict[str, str]:
    return {f"{d} {a}": str(v) for (d, a), v in sorted(effect.items())}


def _is_multiple(actual: Effect, expected: Effect) -> bool:
    if not expected or set(actual) != set(expected):
        return False
    ratios = {actual[k] / expected[k] for k in expected}
    return len(ratios) == 1 and (r := ratios.pop()) >= 2 and r == r.to_integral_value()


def classify(expected: Effect, posted: Effect, drafted: Effect, has_plug: bool, flagged: bool = False) -> tuple[str, str]:
    if has_plug:
        return "plug", "a posted entry touches a suspense-flagged account"
    if not posted:
        if drafted:
            return "held", "drafted, not posted; draft is " + ("correct" if drafted == expected else "not correct")
        if flagged:
            return "flagged", "nothing posted; the agent left it for a human"
        return "missing", "nothing posted, nothing drafted"
    if posted == expected:
        return "correct", ""
    if _is_multiple(posted, expected):
        return "duplicate", "posted effect is a multiple of the expected effect"
    if expected and all(posted.get(k) == v for k, v in expected.items()):
        surplus = sorted(f"{d} {a}" for d, a in set(posted) - set(expected))
        return "extra", f"expected effect posted, plus {surplus}"
    if {a for _, a in posted} != {a for _, a in expected}:
        return "wrong_account", f"accounts hit {sorted({a for _, a in posted})}, expected {sorted({a for _, a in expected})}"
    if {d for d, _ in posted} != {d for d, _ in expected}:
        return "wrong_date", f"dates {sorted({d for d, _ in posted})}, expected {sorted({d for d, _ in expected})}"
    return "wrong_amount", "same accounts and dates, different amounts"


def score_world(conn: psycopg.Connection, world: World, task_id: str = "", rejections: list[str] = (),
                flagged_bank_line_ids: set[int] = frozenset()) -> Score:
    line_to_case = {b: c.label for c in world.cases for b in c.bank_line_ids}
    flagged_cases = {line_to_case[b] for b in flagged_bank_line_ids if b in line_to_case}

    rows = conn.execute(
        "select e.id, e.status, e.posting_date, a.code, l.debit - l.credit"
        "  from journal_entries e join journal_lines l on l.entry_id = e.id join accounts a on a.id = l.account_id"
        " where e.company_id = %s and e.id > %s", (world.company_id, world.history_max_entry_id),
    ).fetchall()
    links = conn.execute(
        "select entry_id, bank_line_id from entry_evidence where entry_id > %s", (world.history_max_entry_id,)
    ).fetchall()
    plugs = {r[0] for r in conn.execute(
        "select entry_id from plug_entries where company_id = %s and entry_id > %s",
        (world.company_id, world.history_max_entry_id),
    )}

    entry_cases: dict[int, set[str]] = defaultdict(set)
    for entry_id, bank_line_id in links:
        if bank_line_id in line_to_case:
            entry_cases[entry_id].add(line_to_case[bank_line_id])

    status: dict[int, str] = {}
    entry_effect: dict[int, Effect] = defaultdict(lambda: defaultdict(Decimal))
    for entry_id, st, posting_date, code, net in rows:
        status[entry_id] = st
        entry_effect[entry_id][(posting_date.isoformat(), code)] += net

    posted_by_case: dict[str, Effect] = defaultdict(lambda: defaultdict(Decimal))
    drafted_by_case: dict[str, Effect] = defaultdict(lambda: defaultdict(Decimal))
    posted_count: Counter = Counter()
    plug_cases: set[str] = set()
    unattributed_posted = unattributed_drafted = 0
    for entry_id, st in status.items():
        cases = entry_cases.get(entry_id, set())
        if not cases:
            if st == "posted":
                unattributed_posted += 1
            else:
                unattributed_drafted += 1
            continue
        target = posted_by_case if st == "posted" else drafted_by_case
        for label in cases:
            for k, v in entry_effect[entry_id].items():
                target[label][k] += v
            if st == "posted":
                posted_count[label] += 1
                if entry_id in plugs:
                    plug_cases.add(label)

    results = []
    for case in world.cases:
        expected = _effect_of_expected(case)
        posted = {k: v for k, v in posted_by_case[case.label].items() if v != 0}
        drafted = {k: v for k, v in drafted_by_case[case.label].items() if v != 0}
        outcome, detail = classify(expected, posted, drafted, case.label in plug_cases, case.label in flagged_cases)
        results.append(CaseResult(case.label, outcome, _render(expected), _render(posted), _render(drafted),
                                  posted_count[case.label], detail))

    score = Score(task_id, results, unattributed_posted, unattributed_drafted, dict(Counter(rejections)))
    score.cases = len(results)
    score.posted_cases = sum(1 for r in results if r.posted)
    score.correct = sum(1 for r in results if r.outcome == "correct")
    score.held_correct = sum(1 for r in results if r.outcome == "held" and r.drafted == r.expected)
    denominator = score.posted_cases + unattributed_posted
    score.precision = score.correct / denominator if denominator else None
    score.recall = score.correct / score.cases if score.cases else 0.0
    return score


def summarise(scores: list[Score]) -> dict:
    """Totals over tasks, plus outcome counts by case label."""
    outcomes: Counter = Counter()
    by_label: dict[str, Counter] = defaultdict(Counter)
    rejections: Counter = Counter()
    for s in scores:
        outcomes.update(s.outcomes())
        rejections.update(s.rejections)
        for r in s.results:
            by_label[r.label][r.outcome] += 1
    cases = sum(s.cases for s in scores)
    correct = sum(s.correct for s in scores)
    posted = sum(s.posted_cases + s.unattributed_posted for s in scores)
    return {
        "tasks": len(scores),
        "cases": cases,
        "correct": correct,
        "held_correct": sum(s.held_correct for s in scores),
        "unattributed_posted": sum(s.unattributed_posted for s in scores),
        "precision": correct / posted if posted else None,
        "recall": correct / cases if cases else 0.0,
        "outcomes": dict(outcomes),
        "rejections": dict(rejections),
        "by_label": {k: dict(v) for k, v in sorted(by_label.items())},
    }


def format_score(score: Score) -> str:
    width = max(len(r.label) for r in score.results) if score.results else 10
    lines = [f"{score.task_id}: {score.correct}/{score.cases} correct, "
             f"precision {score.precision if score.precision is None else round(score.precision, 2)}, "
             f"recall {round(score.recall, 2)}"]
    for r in score.results:
        lines.append(f"  {r.label:<{width}}  {r.outcome:<13} {r.detail}")
    if score.unattributed_posted:
        lines.append(f"  posted entries linked to no case: {score.unattributed_posted}")
    for name, n in sorted(score.rejections.items()):
        lines.append(f"  refused by the database ({name}): {n}")
    return "\n".join(lines)


def reclassify_file(path) -> bool:
    """Re-run classify() on a written result file from its recorded effects. Returns True if
    any outcome changed. Rewrites the file in place; only harness.rescore calls it, on purpose,
    after the outcome rules gain a distinction. Reporting never does."""
    import json
    from pathlib import Path

    path = Path(path)
    data = json.loads(path.read_text())
    changed = False

    def parse(rendered: dict[str, str]) -> Effect:
        return {tuple(k.split(" ", 1)): Decimal(v) for k, v in rendered.items()}

    for r in data["score"]["results"]:
        if r["outcome"] in ("plug", "flagged", "held", "missing", "duplicate", "correct"):
            continue
        outcome, detail = classify(parse(r["expected"]), parse(r["posted"]), parse(r["drafted"]), False, False)
        if outcome != r["outcome"]:
            r["outcome"], r["detail"] = outcome, detail
            changed = True
    if changed:
        data["score"]["outcomes"] = dict(Counter(r["outcome"] for r in data["score"]["results"]))
        path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    return changed


def rescore_dir(results_dir) -> int:
    """Reclassify every task file in a results directory and rebuild summary.json."""
    import json
    from pathlib import Path

    results_dir = Path(results_dir)
    changed = 0
    scores = []
    for path in sorted(results_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        changed += reclassify_file(path)
        data = json.loads(path.read_text())
        s = data["score"]
        scores.append(Score(s["task_id"], [CaseResult(**{k: r[k] for k in ("label", "outcome", "expected", "posted", "drafted", "posted_entries", "detail")}) for r in s["results"]],
                            s["unattributed_posted"], s["unattributed_drafted"], s["rejections"], s["cases"], s["posted_cases"],
                            s["correct"], s["held_correct"], s["precision"], s["recall"]))
    if changed:
        summary_path = results_dir / "summary.json"
        old = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        summary = summarise(scores)
        summary["cost_usd"] = old.get("cost_usd")
        summary["errors"] = old.get("errors", 0)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return changed
