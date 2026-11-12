# `petermakhnatch/transaction-reconciliation`

This task measures whether an agent can reconcile a SQLite ledger against its settlement feed without changing unrelated data.
The agent matches entries by `external_id`, corrects settlement amounts, and marks mismatched entries as `reconciled`.
See [instruction.md](instruction.md) for the contract.

## Environment

The agent image builds from `python:3.12-slim-bookworm` in `environment/Dockerfile`.
It installs the `sqlite3` command and uses `/app` as its working directory.
Python also provides its standard `sqlite3` module.
The build copies `seed_db.py` to `/tmp/seed_db.py` and runs it to create `/app/data/ledger.db`.
The Dockerfile does not define an entrypoint.

The database contains `ledger_entries` and `settlement_feed`, each with 6 entries.
One ledger entry has an incorrect settlement amount and a `pending` status.
The agent must edit the database directly.
It must preserve identifiers, expected amounts, the settlement feed, the schema, and entries that already match.
The reference solution uses one SQL transaction to update every mismatched entry from its matching feed entry.

`task.toml` sets the agent timeout to 120 seconds, the image build timeout to 600 seconds, and `network_mode` to `public`.
It does not set `cpus`, `memory_mb`, or `storage_mb`, so this package does not pin those resource limits.
The only declared artifact is `/app/data/ledger.db`.

## Verifier

`environment_mode` is `separate`, with a verifier timeout of 120 seconds.
The verifier builds a separate image from `tests/Dockerfile`, also based on `python:3.12-slim-bookworm`.
It does not run in the agent container.
The image copies the verifier files into `/tests`; only the declared database artifact comes from the agent.
`tests/test.sh` runs `/usr/local/bin/python3 -I /tests/verify.py` without installing or fetching dependencies.
The verifier opens the database read-only and compares its contents against expectations stored in `tests/verify.py`.

| Check | Requirement |
|---|---|
| `ledger` | Ledger rows exactly match the reconciled entries, with no unrelated data changes. |
| `feed` | Settlement feed rows exactly match the original entries. |
| `schema` | Column names and their order match expectations for both tables. |

A missing database, a symlink, or a SQLite error fails all checks.
The verifier writes `/logs/verifier/ctrf.json` with the check results and `/logs/verifier/reward.txt` with the binary reward.
The reward is `1` only when every check passes; otherwise, it is `0`.
Expected rewards are `1` for Oracle and `0` for nop.

## Layout

```text
README.md                   Task overview and run commands
instruction.md              Agent-visible reconciliation contract
task.toml                   Timeouts, network mode, and artifact boundary
.gitignore                  Local build, cache, and secret exclusions
environment/                Agent image build files
  Dockerfile                Python image with the sqlite3 command
  seed_db.py                Initial ledger and settlement feed
solution/                   Reference solution
  solve.sh                  Transactional SQL reconciliation
tests/                      Separate verifier image and checks
  Dockerfile                Verifier image definition
  test.sh                   Isolated Python verifier runner
  verify.py                 Database checks and verifier reports
```

## Run

```bash
uv run evallab run --task library/tasks/transaction-reconciliation --agent oracle --name transaction-reconciliation-oracle
uv run evallab run --task library/tasks/transaction-reconciliation --agent nop --name transaction-reconciliation-nop
```

Expected rewards are `1` for Oracle and `0` for nop.
