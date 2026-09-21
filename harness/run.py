"""Run an agent over tasks, score each, write results, print the table.

    .venv/bin/python -m harness.run baseline                        # all 27 tasks
    .venv/bin/python -m harness.run baseline s1-part_payment s1-whole-month
    .venv/bin/python -m harness.run llm:claude-opus-5 s1-whole-month
    .venv/bin/python -m harness.run llm:claude-sonnet-5:medium      # model:effort
    .venv/bin/python -m harness.run llm:claude-sonnet-5 --out=results/claude-sonnet-5-high-repeat

Results land in results/<agent>/<task>.json (score, tool calls, flags, refusals, traces,
usage and cost) and results/<agent>/summary.json. ANTHROPIC_API_KEY comes from the
environment or from a .env file in the repo root.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from harness import db, tasks
from harness.scorer import Score, format_score, score_world, summarise
from harness.tools import Toolbox

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def run_task(owner, reviewer, agent_conn, agent, task: tasks.Task) -> tuple[Score, Toolbox, float, str | None]:
    """Build, run, score. An agent that raises still gets scored on what it did; the error is kept."""
    world = tasks.build_task_world(owner, reviewer, task)
    tools = Toolbox(agent_conn, reviewer, world.company_id)
    started = time.perf_counter()
    error = None
    try:
        agent.run(tools, tasks.instructions_for(world))
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        agent_conn.rollback()
        reviewer.rollback()
    elapsed = time.perf_counter() - started
    flagged = {b for f in tools.flags for b in f["bank_line_ids"]}
    score = score_world(owner, world, task.id, tools.refusals, flagged)
    return score, tools, elapsed, error


def run(agent, task_list: list[tasks.Task], out_dir: Path | None = None, quiet: bool = False) -> list[Score]:
    out_dir = out_dir or RESULTS_DIR / agent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    scores, costs, errors = [], [], []
    with db.connect() as owner, db.connect(db.REVIEWER) as reviewer, db.connect(db.AGENT) as agent_conn:
        for task in task_list:
            score, tools, elapsed, error = run_task(owner, reviewer, agent_conn, agent, task)
            scores.append(score)
            usage = getattr(agent, "usage", None)
            record = {
                "task": task.id,
                "agent": agent.name,
                "seconds": round(elapsed, 3),
                "error": error,
                "score": score.as_dict(),
                "usage": usage.as_dict(agent.model) if usage else None,
                "flags": tools.flags,
                "traces": tools.traces,
                "calls": [{"tool": c.tool, "args": c.args, "result": c.result} for c in tools.calls],
                "summary": getattr(agent, "summary", ""),
                "transcript": getattr(agent, "transcript", None),
            }
            (out_dir / f"{task.id}.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
            costs.append(usage.cost_usd(agent.model) if usage else None)
            errors.append(error)
            if not quiet:
                line = format_score(score)
                if usage:
                    line += f"\n  {usage.requests} requests, {usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens} in, {usage.output_tokens} out, ${usage.cost_usd(agent.model)}, {elapsed:.0f}s"
                if error:
                    line += f"\n  ERROR {error}"
                print(line, flush=True)
    summary = summarise(scores)
    known = [c for c in costs if c is not None]
    summary["cost_usd"] = str(sum(known)) if known else None
    summary["errors"] = sum(1 for e in errors if e)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if not quiet:
        print(format_summary(summary))
    return scores


def format_summary(summary: dict) -> str:
    p = summary["precision"]
    lines = [
        "",
        f"{summary['tasks']} tasks, {summary['cases']} cases: {summary['correct']} correct, "
        f"precision {p if p is None else round(p, 3)}, recall {round(summary['recall'], 3)}, "
        f"held correct {summary['held_correct']}, unattributed posts {summary['unattributed_posted']}",
        f"refusals: {summary['rejections'] or 'none'}; cost ${summary.get('cost_usd')}; task errors {summary.get('errors', 0)}",
        "",
        f"{'case':<34} " + " ".join(f"{o:>13}" for o in OUTCOME_ORDER),
    ]
    for label, counts in summary["by_label"].items():
        lines.append(f"{label:<34} " + " ".join(f"{counts.get(o, 0):>13}" for o in OUTCOME_ORDER))
    return "\n".join(lines)


OUTCOME_ORDER = ("correct", "flagged", "held", "missing", "extra", "wrong_account", "wrong_date", "wrong_amount", "duplicate", "plug")


def agent_by_name(name: str):
    if name == "baseline":
        from harness.baseline import BaselineAgent
        return BaselineAgent()
    if name == "fee-only":
        from harness.fee_only import FeeOnlyAgent
        return FeeOnlyAgent()
    if name.startswith("llm:"):
        from harness.llm import LLMAgent
        _, model, *rest = name.split(":")
        return LLMAgent(model=model, effort=rest[0] if rest else "high")
    raise SystemExit(f"unknown agent {name!r}")


def load_dotenv(path: Path = Path(__file__).resolve().parent.parent / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    load_dotenv()
    agent = agent_by_name(sys.argv[1])
    out_dir = None
    wanted = set()
    for arg in sys.argv[2:]:
        if arg.startswith("--out="):
            out_dir = Path(arg.removeprefix("--out="))
        elif arg == "--out":
            raise SystemExit("use --out=<dir>")
        else:
            wanted.add(arg)
    chosen = [t for t in tasks.load_tasks() if not wanted or t.id in wanted]
    run(agent, chosen, out_dir=out_dir)
