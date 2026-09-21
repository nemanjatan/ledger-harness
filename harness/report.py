"""One table across every agent that has results: `.venv/bin/python -m harness.report`.

Reads results/<agent>/summary.json and the per-task files for cost and errors, and writes
results/comparison.md so the write-up can include it verbatim. It never changes a result
file: results are the record of a run. To reclassify outcomes after a scorer change, run
`python -m harness.rescore results/<agent>` deliberately and commit the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.run import OUTCOME_ORDER, RESULTS_DIR


def load(results_dir: Path = RESULTS_DIR) -> list[dict]:
    rows = []
    for summary_path in sorted(results_dir.glob("*/summary.json")):
        agent = summary_path.parent.name
        summary = json.loads(summary_path.read_text())
        tasks = [json.loads(p.read_text()) for p in sorted(summary_path.parent.glob("*.json")) if p.name != "summary.json"]
        seconds = sum(t.get("seconds", 0) for t in tasks)
        requests = sum((t.get("usage") or {}).get("requests", 0) for t in tasks)
        rows.append({
            "agent": agent,
            "tasks": summary["tasks"],
            "cases": summary["cases"],
            "correct": summary["correct"],
            "precision": summary["precision"],
            "recall": summary["recall"],
            "outcomes": summary["outcomes"],
            "refusals": summary["rejections"],
            "unattributed": summary["unattributed_posted"],
            "errors": summary.get("errors", 0),
            "cost_usd": summary.get("cost_usd"),
            "requests": requests,
            "seconds": round(seconds),
            "by_label": summary["by_label"],
        })
    return rows


def fmt(value, digits=3):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def markdown(rows: list[dict]) -> str:
    out = ["| agent | cases | correct | precision | recall | flagged | held | extra | wrong | dup | plug | refusals | errors | cost USD | time |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        o = r["outcomes"]
        wrong = sum(o.get(k, 0) for k in ("wrong_account", "wrong_date", "wrong_amount"))
        refusals = ", ".join(f"{k} {v}" for k, v in sorted(r["refusals"].items())) or "0"
        out.append(f"| {r['agent']} | {r['cases']} | {r['correct']} | {fmt(r['precision'])} | {fmt(r['recall'])} | "
                   f"{o.get('flagged', 0)} | {o.get('held', 0)} | {o.get('extra', 0)} | {wrong} | {o.get('duplicate', 0)} | {o.get('plug', 0)} | "
                   f"{refusals} | {r['errors']} | {fmt(r['cost_usd'])} | {r['seconds'] // 60}m{r['seconds'] % 60:02d}s |")
    out.append("")
    labels = sorted({l for r in rows for l in r["by_label"]})
    if labels and len(rows) > 1:
        out.append("Correct per case label (out of the times it appeared):")
        out.append("")
        out.append("| case | " + " | ".join(r["agent"] for r in rows) + " |")
        out.append("|---|" + "---|" * len(rows))
        for label in labels:
            cells = []
            for r in rows:
                counts = r["by_label"].get(label, {})
                cells.append(f"{counts.get('correct', 0)}/{sum(counts.values())}")
            out.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    rows = load()
    text = markdown(rows)
    (RESULTS_DIR / "comparison.md").write_text(text)
    print(text)
