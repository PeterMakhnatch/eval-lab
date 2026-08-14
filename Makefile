.PHONY: help sync check db-up db-down db-init doctor controls ingest summarize

help:
	@echo "sync       Install locked Python dependencies"
	@echo "check      Run lint and tests"
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

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

db-init:
	uv run harbor-lab db init

doctor:
	uv run harbor-lab doctor

controls:
	uv run harbor-lab matrix experiments/local-controls.json

ingest:
	uv run harbor-lab ingest runs research/evidence/runs

summarize:
	uv run harbor-lab summarize runs research/evidence/runs
