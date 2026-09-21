"""The task set.

A task is a seed and a list of case labels. The world is regenerated from the seed at run time
with only those cases' evidence, so a task file is small and the expected end state in it is
informational: it is what the generator will produce again, and a test checks that.

The set: every case on its own for seeds 1 and 2 (24 tasks) plus the whole month for seeds 1,
2 and 3 (3 tasks). The single-case tasks say which failure classes an agent handles; the
whole-month tasks say whether it still does when they all arrive at once.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import psycopg

from harness import db
from harness.world import LABELS, World, build_world

TASKS_DIR = Path(__file__).resolve().parent.parent / "tasks"

INSTRUCTIONS = """\
You are the bookkeeper for {company}. Today is {today}. Some bank lines are not yet recorded in
the ledger. Record what should be recorded, and only that.

Rules:
- Post on the bank line's date. If that date falls in a locked period, post on the first day of
  the earliest open period and put the bank date in the memo.
- Never post to a suspense or miscellaneous account to make a difference disappear.
- A receipt from a customer settles receivables; a payment of a bill settles payables. A payment
  with no bill is an expense on the vendor's default account.
- Link every entry to the bank line or lines it records. If a line needs no entry of its own
  (a duplicate, a pending line that has since cleared), flag it and name the entry that covers
  it. Links are added when an entry is drafted; a posted entry cannot take new links.
- One invoice may be unpaid with no evidence at all. Post nothing for it.
"""


@dataclass(frozen=True)
class Task:
    id: str
    seed: int
    labels: tuple[str, ...]
    expected: list[dict]        # the cases the generator produced, as plain data


def task_set() -> list[tuple[str, int, list[str]]]:
    singles = [(f"s{seed}-{label}", seed, [label]) for seed in (1, 2) for label in LABELS]
    months = [(f"s{seed}-whole-month", seed, list(LABELS)) for seed in (1, 2, 3)]
    return singles + months


def build_task_world(owner: psycopg.Connection, reviewer: psycopg.Connection, task: Task) -> World:
    db.reset_data(owner)
    return build_world(owner, reviewer, task.seed, list(task.labels))


def instructions_for(world: World) -> str:
    from harness.world import COMPANY
    return INSTRUCTIONS.format(company=COMPANY, today=world.today.isoformat())


def render(task_id: str, world: World) -> dict:
    return {
        "id": task_id,
        "seed": world.seed,
        "labels": [c.label for c in world.cases],
        "expected": json.loads(world.truth_json())["cases"],
    }


def write_tasks(owner: psycopg.Connection, reviewer: psycopg.Connection, out_dir: Path = TASKS_DIR) -> list[Path]:
    out_dir.mkdir(exist_ok=True)
    written = []
    for task_id, seed, labels in task_set():
        db.reset_data(owner)
        world = build_world(owner, reviewer, seed, labels)
        path = out_dir / f"{task_id}.json"
        path.write_text(json.dumps(render(task_id, world), indent=2) + "\n")
        written.append(path)
    return written


def load_task(path: Path) -> Task:
    data = json.loads(path.read_text())
    return Task(data["id"], data["seed"], tuple(data["labels"]), data["expected"])


def load_tasks(dir: Path = TASKS_DIR) -> list[Task]:
    return [load_task(p) for p in sorted(dir.glob("*.json"))]


if __name__ == "__main__":
    with db.connect() as owner, db.connect(db.REVIEWER) as reviewer:
        for path in write_tasks(owner, reviewer):
            print(path.relative_to(TASKS_DIR.parent))
