.PHONY: help sync check premerge db-up db-down db-init doctor controls ingest summarize

help:
	@echo "sync       Install locked Python dependencies"
	@echo "check      Run lint and tests"
	@echo "premerge   Mirror the complete CI gate on Python 3.12"
	@echo "db-up      Start local PostgreSQL"
	@echo "db-down    Stop local PostgreSQL (preserves volume)"
	@echo "db-init    Apply the idempotent database schema"
	@echo "controls   Run Oracle and no-op controls"
	@echo "ingest     Ingest raw and curated Harbor jobs"
	@echo "summarize  Print a Markdown result table"

sync:
	uv sync --frozen

check:
	uv run ruff check .
	uv run pytest

premerge:
	scripts/premerge.sh

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

db-init:
	uv run evallab db init

doctor:
	uv run evallab doctor

controls:
	uv run evallab matrix research/experiments/local-controls.json

ingest:
	uv run evallab ingest runs research/evidence/runs

summarize:
	uv run evallab summarize runs research/evidence/runs
