#!/usr/bin/env sh
# One command: start Postgres in Docker (if needed), bootstrap the venv (if needed), run the suite.
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/pytest ]; then
    python3.13 -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -e '.[dev]'
fi
docker compose up -d --wait
exec .venv/bin/pytest "$@"
