# `terminal-bench/query-optimize`

This task tests SQL optimization, SQLite syntax, and exact result preservation on the Open English Wordnet (OEWN) database.
The agent rewrites a query with correlated subqueries while preserving its output.
See [instruction.md](instruction.md).

## Environment

The configured image is `alexgshaw/query-optimize:20251031`.
The local `environment/Dockerfile` uses `ubuntu:24.04` and installs `sqlite3` and `curl`.
The environment has 1 CPU, 2048 MB RAM, 10240 MB storage, and no GPU.
The agent timeout is 900 seconds; the image build timeout is 600 seconds.
Network access is enabled through `allow_internet = true`.

The container starts in `/app` with the OEWN database at `/app/oewn.sqlite` and the original query at `/app/my-sql-query.sql`.
The Dockerfile downloads the database and sets its permissions to `444`; it does not mount a separate read-only volume.
The agent must preserve the database and write SQLite SQL to `/app/sol.sql`.

The original query uses correlated subqueries to select each word's top synset and count its senses.
The reference solution replaces these subqueries with CTEs and a `ROW_NUMBER()` window function.
This tests joins, aggregation, query rewriting, and result ordering without adding indexes or changing the database.

## Verifier

Verification uses the shared, in-container environment, not a separate verifier image.
`task.toml` leaves `environment_mode` unset; this package has no `tests/Dockerfile`.
The verifier timeout is 1800 seconds.

`tests/test.sh` runs inside the agent container.
It installs `curl` and `uv` 0.9.5, then uses `uvx` with Python 3.13, pytest 8.4.1, and pytest-json-ctrf 0.3.5.
It runs `/tests/test_outputs.py` against `/app/sol.sql`, the database, the original query, and `/tests/golden.sql`.

| Check | Requirement |
|---|---|
| Original query | Its ordered result rows match the golden query. |
| Database integrity | The database SHA-256 matches the fixed expected hash. |
| Solution output | Column names, column order, row count, values, and row order match the golden query exactly. |
| Performance | The solution's median runtime is at most `1.05` times the golden query's median runtime. |
| SQL format | After comment removal and whitespace normalization, SQL starts with `SELECT` or `WITH` and contains exactly one semicolon, at the end. |
| SQL validity | SQLite accepts `EXPLAIN QUERY PLAN` for the submitted SQL using a read-only connection. |
| Size | The UTF-8 text, after stripping leading and trailing whitespace, contains at most 2000 characters (`len(sql_content) <= 2000`). |

The format check removes `--` line comments and `/* ... */` block comments before checking the statement.
It does not forbid comments, although `instruction.md` requests comment-free SQL.
The size check still counts comments and internal whitespace.

The performance check warms both queries, then measures 5 iterations with alternating execution order.
It fetches all rows during each timed execution and compares median runtimes.
It prints timing statistics; it does not write a benchmark summary file.

The verifier writes the CTRF report to `/logs/verifier/ctrf.json`.
It writes `1` to `/logs/verifier/reward.txt` when pytest succeeds, or `0` when pytest fails.
Expected rewards are `1` for Oracle and `0` for nop.

## Layout

```text
query-optimize/
├── README.md                            # Package guide
├── instruction.md                       # Agent-visible task contract
├── task.toml                            # Task metadata, image, limits, and network setting
├── environment/                         # Agent image build context
│   ├── Dockerfile                       # Ubuntu base, tools, and database setup
│   └── task-deps/                       # Input query build dependency
│       └── my-sql-query.sql              # Original query with correlated subqueries
├── solution/                            # Oracle reference solution
│   └── solve.sh                         # Writes the optimized query to /app/sol.sql
└── tests/                               # Shared, in-container verifier files
    ├── golden.sql                       # Reference SQL for output and timing checks
    ├── test.sh                          # Runs pytest and writes report and reward
    └── test_outputs.py                  # Correctness, timing, format, size, and integrity checks
```

## Run

```bash
uv run evallab run --task library/tasks/query-optimize --agent oracle --name query-optimize-oracle
uv run evallab run --task library/tasks/query-optimize --agent nop --name query-optimize-nop
```

Expected rewards are `1` for Oracle and `0` for nop.