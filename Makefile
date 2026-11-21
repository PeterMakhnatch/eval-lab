.PHONY: help sync gate land smoke db-up db-down db-init doctor controls ingest summarize line-replay line-gate

help:
	@echo "sync       Install locked Python dependencies"
	@echo "gate       Run local checks and deterministic simulation"
	@echo "land       Rebase, run the gate, and push to main under the landing lock"
	@echo "smoke      Run the full local doctor/Harbor/Postgres/Parquet/digest smoke"
	@echo "db-up      Start local PostgreSQL"
	@echo "db-down    Stop local PostgreSQL (preserves volume)"
	@echo "db-init    Apply the idempotent database schema"
	@echo "controls   Run Oracle and no-op controls"
	@echo "ingest     Ingest raw and curated Harbor jobs"
	@echo "summarize  Print a Markdown result table"
	@echo "line-replay Run the frozen replay-v1 assembly-line gate (exit 77 = data missing, skipped)"
	@echo "line-gate  Compare base/head full DEV line; ARGS='--output PATH' (0 pass, 1 block, 2 error)"

sync:
	uv sync --frozen

gate:
	scripts/gate

land:
	scripts/land

smoke:
	uv run python -m evallab.smoke

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

line-replay:
	uv run python scripts/line-replay.py $(LINE_REPLAY_ARGS) $(if $(J3_CACHE),--j3-cache "$(J3_CACHE)") $(if $(GOODFIRE_REVIEW_FLOOR),--goodfire-review-floor "$(GOODFIRE_REVIEW_FLOOR)")

line-gate:
	uv run python scripts/line-gate.py $(ARGS)
