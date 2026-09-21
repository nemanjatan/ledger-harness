"""The scripted baseline, end to end through the runner."""

import json

from harness import tasks
from harness.baseline import BaselineAgent
from harness.run import run
from harness.scorer import score_world
from harness.tools import Toolbox
from harness.world import build_world


def test_baseline_on_the_whole_month(conn, agent, reviewer):
    world = build_world(conn, reviewer, seed=1)
    tools = Toolbox(agent, reviewer, world.company_id)
    BaselineAgent().run(tools)
    flagged = {b for f in tools.flags for b in f["bank_line_ids"]}
    score = score_world(conn, world, "s1", tools.refusals, flagged)
    outcomes = {r.label: r.outcome for r in score.results}
    assert outcomes.pop("intercompany_transfer") == "flagged"
    assert set(outcomes.values()) == {"correct"}
    assert score.precision == 1.0 and score.rejections == {}
    assert conn.execute("select count(*) from plug_entries").fetchone()[0] == 0


def test_runner_writes_results(tmp_path):
    chosen = [t for t in tasks.load_tasks() if t.id in ("s2-part_payment", "s3-whole-month")]
    scores = run(BaselineAgent(), chosen, out_dir=tmp_path, quiet=True)
    assert [s.task_id for s in scores] == ["s2-part_payment", "s3-whole-month"]
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["tasks"] == 2 and summary["cases"] == 13
    single = json.loads((tmp_path / "s2-part_payment.json").read_text())
    assert single["score"]["outcomes"] == {"correct": 1}
    assert any(c["tool"] == "post_entry" for c in single["calls"])


def test_fee_only_example_agent(conn, agent, reviewer):
    from harness.fee_only import FeeOnlyAgent
    world = build_world(conn, reviewer, seed=1, labels=["bank_fee", "part_payment"])
    tools = Toolbox(agent, reviewer, world.company_id)
    FeeOnlyAgent().run(tools)
    flagged = {b for f in tools.flags for b in f["bank_line_ids"]}
    outcomes = {r.label: r.outcome for r in score_world(conn, world, "t", tools.refusals, flagged).results}
    assert outcomes == {"bank_fee": "correct", "part_payment": "flagged"}
