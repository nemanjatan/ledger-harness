"""Reclassify the outcomes in one results directory from the effects each task recorded.

    .venv/bin/python -m harness.rescore results/claude-sonnet-5-high

A one-off, run on purpose after the scorer's outcome rules change, never as part of
reporting. It rewrites the task files and summary.json in place; review and commit the diff.
"""

import sys
from pathlib import Path

from harness.scorer import rescore_dir

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    target = Path(sys.argv[1])
    if not (target / "summary.json").exists():
        raise SystemExit(f"{target} has no summary.json")
    changed = rescore_dir(target)
    print(f"{changed} task file(s) reclassified in {target}")
