---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Unified Attach Surface (E04)

The single mandated entry point for all consumers of the three storage zones.

## Zones

- **z2**: PostgreSQL catalog via `postgres_scanner`. Tables mirror the canonical entities (§2.1). Unavailable when `DATABASE_URL` cannot be reached; reason includes the DSN identity examined.
- **z3**: Parquet analytics. Views `trial_facts`, `reward_facts`, `artifact_facts`, `trajectories`, `steps`, `tool_calls`, `tool_usage`, `observations`, `jobs`. Hot layout `job_id=*/trial_id=*/<table>.parquet`; cold `compact/<table>/dt=YYYY-MM-DD/part*.parquet`. Uses `union_by_name=true`. Unavailable when derived root does not exist.
- **z4**: Knowledge front-matter. Table `front_matter` with columns `path`, `title`, `status`, `audience`, `generated_by`. Populated via `contextpack.parse_doc`. Unavailable when `docs/` missing.

## Python usage

```python
from evallab.attach import attach
result = attach()
print(result.zones)
rows = result.connection.execute("SELECT * FROM z3.trial_facts LIMIT 5").fetchall()
result.connection.close()
```

## Bare duckdb shell

```sql
INSTALL postgres_scanner;
LOAD postgres_scanner;
ATTACH 'postgresql://...' AS z2 (TYPE postgres);
CREATE SCHEMA IF NOT EXISTS z3;
CREATE SCHEMA IF NOT EXISTS z4;
CREATE OR REPLACE VIEW z3.trial_facts AS SELECT * FROM read_parquet([...], union_by_name=true);
...
```

## Unavailable vs empty

A zone reports `attached=False` with explicit `reason` and the path/DSN examined. A silent empty view is never produced.

## Cross-zone example (Z2 + Z3)

```sql
SELECT j.status, COUNT(*) AS trials
FROM z2.jobs j
JOIN z3.trial_facts t ON j.id = t.job_id
GROUP BY j.status;
```

When Postgres is unavailable the surface still returns a usable connection carrying Z3 and Z4; the join is skipped with explicit reason naming the DSN.
