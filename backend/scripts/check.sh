#!/bin/sh
# All backend checks. Runs inside the rec-check container, or on the host via uv
# (database tests are skipped there without REC_TEST_DATABASE_URL).
set -eu
cd "$(dirname "$0")/.."
uv sync --frozen --quiet
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest -q
