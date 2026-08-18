---
status: living
audience:
  - builder
  - operator
---

# Catalog Tables and Views: Suites, Suite Members, and Quota

This document specifies the Z2 catalog entities added to fulfill `docs/platform-architecture.md` §2.2, §2.3, §3.1, and §4:
1. `suites` — Named collections of task versions, freezable for immutable benchmark comparisons.
2. `suite_members` — Membership relation linking suites to specific `TaskVersion` references (`task_ref`, `task_version`).
3. `v_quota_today` — View aggregating provider consumption (runs and tokens) for the current UTC day.

---

## 1. Catalog Tables: `suites` and `suite_members`

### Schema Definition

```sql
CREATE TABLE IF NOT EXISTS suites (
    name text NOT NULL,
    version text NOT NULL,
    frozen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS suite_members (
    suite_name text NOT NULL,
    suite_version text NOT NULL,
    task_ref text NOT NULL,
    task_version text NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (suite_name, suite_version, task_ref, task_version),
    FOREIGN KEY (suite_name, suite_version) REFERENCES suites(name, version) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS suite_members_task_idx ON suite_members (task_ref, task_version);
CREATE INDEX IF NOT EXISTS suite_members_suite_idx ON suite_members (suite_name, suite_version);
```

### Purpose and Contracts

- **Entity identity**: A suite is identified by `(name, version)` (e.g. `canary@v1`, `core-battery@2026-08`).
- **Membership**: Links a suite to exact `TaskVersion` instances `(task_ref, task_version)`. A task may appear in multiple suites or versions.
- **Spine integration**: Supports §2.1 join spine `task_ref@version → {craft_record, suite_member, registry entry}`.

---

## 2. Frozen Suite Immutability

### Contract (§2.1, §4)

A suite in draft state (`frozen_at IS NULL`) accepts membership additions, modifications, and deletions. Once a suite is frozen (`frozen_at IS NOT NULL`), its membership is **immutable forever**.

Comparisons across dates must cite a frozen suite. If a frozen suite could be silently altered, every historical comparison citing that suite would be corrupted without any trace.

### Why Immutability Lives in the Database

Application-level checks (e.g. in Pydantic models or Python services) protect against accidental modifications within the Python runtime. However:
1. Direct SQL operations (`psycopg`, admin scripts, database migrations, or third-party query tools) would bypass application guards.
2. Data loaded directly into PostgreSQL from external sources could corrupt frozen historical baselines.
3. Concurrency races between freeze operations and member inserts can only be serialized safely at the database engine level.

Enforcing immutability via database triggers guarantees that **no operation** (`INSERT`, `UPDATE`, or `DELETE`) on `suite_members` or `suites` can violate the invariant, regardless of client or entry point.

### Database Enforcement Mechanism

Two `BEFORE` triggers in PostgreSQL enforce immutability:

1. **`trg_suite_members_immutability` on `suite_members`**:
   - On `INSERT`, `UPDATE`, or `DELETE`, checks whether `suites.frozen_at` is non-null for the target suite.
   - If non-null, aborts the transaction with `RAISE EXCEPTION 'Cannot modify membership of frozen suite %@% (frozen at %)'`.

2. **`trg_suite_immutability` on `suites`**:
   - On `UPDATE`, prevents changing `name`, `version`, or modifying/nullifying `frozen_at` once set.
   - On `DELETE`, prevents deleting any suite where `frozen_at IS NOT NULL`.

---

## 3. Quota View: `v_quota_today`

### View Definition

```sql
CREATE OR REPLACE VIEW v_quota_today AS
SELECT
    agent_name AS provider,
    count(*) AS runs,
    sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) AS tokens
FROM trials
WHERE started_at IS NOT NULL
  AND (started_at::timestamptz AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date
GROUP BY agent_name
ORDER BY provider;
```

### UTC Day Convention

Quota in `eval-lab` is accounted strictly per provider per **UTC calendar day**:
- Provider subscription allowances reset on fixed provider schedules or UTC midnight boundaries.
- `src/evallab/quota.py` normalizes all timestamps to UTC.
- Bucketing by local server time causes trials near midnight (e.g. 23:30 UTC or 00:30 UTC) to shift into the wrong calendar day depending on server timezone offset (a bug previously observed and fixed in PR #102).
- `(started_at::timestamptz AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date` guarantees deterministic, timezone-invariant daily grouping.

---

## 4. Empty vs Unavailable State Distinctions

Surfaces consuming catalog tables and views must differentiate between two distinct states:

| State | Meaning | Observable Behavior |
|---|---|---|
| **Empty** (`observed`, count = 0) | The catalog and underlying tables exist and were queried successfully, but no trials ran today or no members were added to a draft suite. | Returns 0 rows. Surfaces display an informative empty state (e.g. `No consumption recorded today for UTC date <YYYY-MM-DD>`). |
| **Unavailable** (`unavailable`) | PostgreSQL is unreachable, connection timed out, or required storage zones are not mounted. | Doctor / preflight / gate fails closed (`catalog read failed [unavailable]`). Never silently outputs empty tables or zero spend. |

### Real Source Analysis for `v_quota_today`

- **Real source for runs & tokens**: The `trials` table in PostgreSQL (populated by `evallab db ingest`) records completed trial runs, `agent_name`, `started_at`, `input_tokens`, and `output_tokens`. `v_quota_today` queries this live catalog table directly.
- **Provider allowance / headroom**: Account-wide headroom percentages and resets are observable only from Harbor subscription sidecars (`agent/quota/*.rate-limits.json` / session rollouts) parsed by `src/evallab/quota.py`. Headroom is not stored in a static catalog table because it is account-wide and dynamically observed.
