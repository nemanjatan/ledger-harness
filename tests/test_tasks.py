"""The committed task files are exactly what the generator produces, and each builds."""

import json

import pytest

from harness import tasks
from harness.world import LABELS


def test_task_set_shape():
    ids = [t[0] for t in tasks.task_set()]
    assert len(ids) == 27 and len(set(ids)) == 27
    assert sum(1 for _, _, labels in tasks.task_set() if len(labels) == len(LABELS)) == 3


def test_committed_task_files_match_the_generator(conn, reviewer, tmp_path):
    written = tasks.write_tasks(conn, reviewer, tmp_path)
    committed = {p.name: p.read_text() for p in tasks.TASKS_DIR.glob("*.json")}
    fresh = {p.name: p.read_text() for p in written}
    assert fresh == committed, "run: .venv/bin/python -m harness.tasks"


def test_single_case_task_has_the_same_amounts_as_the_whole_month(conn, reviewer):
    single = tasks.load_task(tasks.TASKS_DIR / "s1-fee_net_payout.json")
    month = tasks.load_task(tasks.TASKS_DIR / "s1-whole-month.json")
    single_case = single.expected[0]
    month_case = next(c for c in month.expected if c["label"] == "fee_net_payout")
    assert single_case["expected"] == month_case["expected"]


def test_a_task_builds_and_its_expected_matches(conn, reviewer):
    task = tasks.load_task(tasks.TASKS_DIR / "s2-late_line_in_locked_period.json")
    world = tasks.build_task_world(conn, reviewer, task)
    assert [c.label for c in world.cases] == list(task.labels)
    assert json.loads(world.truth_json())["cases"] == task.expected
