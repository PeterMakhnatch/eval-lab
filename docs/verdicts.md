---
status: living
audience:
  - operator
  - analyst
---

# Human Verdict Decision Record (§2.1, §2.2, §6)

`digests/DISCOVERIES.md` accumulates draft findings generated during research and analysis loops.
Until a verdict is rendered, findings remain unvalidated and indeterminate. The **verdict loop**
records explicit human judgement on discovery entries, creating an immutable, append-only
audit trail stored in Zone 2 (PostgreSQL catalog) and queryable via DuckDB views.

Entry point: `evallab verdict <discovery_id> <accepted|rejected|needs_evidence|pending> --by <who> [--note ...]`

---

## 1. The Append-Only Rule

Verdicts are strictly append-only:

- **No Overwrites**: Re-evaluating a discovery adds a new row to the `verdicts` table with a fresh timestamp.
  Prior decisions are never updated, deleted, or mutated.
- **Audit History**: Changing a verdict is analytical data, not a replacement. The history captures how
  conclusions evolved as new evidence or calibrating baselines emerged.
- **Current State**: The active disposition for any discovery is deterministically resolved as the latest
  entry by timestamp (`at`).

---

## 2. Status Vocabulary (§2.1)

Every verdict must assign exactly one of the four §2.1 literal status values:

| Status | Operational Meaning | Downstream Action |
|---|---|---|
| `accepted` | The finding is validated by adequate control baselines and uncorrupted evidence. | Promoted for consumption by downstream synthesis and researcher prompts. |
| `rejected` | The claim is refuted, invalid, ungrounded, or caused by harness artifacts. | Excluded from downstream synthesis; recorded as a negative result. |
| `needs_evidence` | The claim is plausible or promising, but underpowered or missing necessary controls. | Kept in draft state; signals that targeted experiment specs should be queued. |
| `pending` | The discovery has been ingested into the journal but awaits human review. | Default initial disposition prior to operator review. |

---

## 3. Mandatory Human Accountability (`--by`)

The `--by` parameter is mandatory and **must be a human name** (e.g. `--by "Peter Makhnatch"`).

- **Purpose**: The verdict record exists to capture human governance and responsibility.
- **Automated Actors Prohibited**: Automated agents, bots, harnesses, and unattended pipelines (such as `autopilot`, `codex`, `agent`, `bot`, `ci`) are strictly refused. An automated verdict would defeat the entire purpose of the human decision table.

### Refusal Messages

- **Empty or Whitespace Actor**:
  ```
  Actor (--by) is required and cannot be empty
  ```
- **Automated Actor**:
  ```
  Automated actor '<by>' refused: verdicts require human judgment (e.g. --by 'Peter Makhnatch')
  ```

---

## 4. Discovery ID Resolution and Validation

To prevent typos from creating orphaned decisions, every `discovery_id` must resolve to an existing
entry in `digests/DISCOVERIES.md`.

### Identifier Convention and Journal Precedence
While platform-architecture v2 §2.1 establishes ULIDs as the default identifier format, `Verdict.discovery_id`
intentionally departs from the ULID convention. The discovery journal format (`digests/DISCOVERIES.md`)
predates the v2 contract model and uses identifiers formatted as `D-YYYYMMDD-SUFFIX` (e.g. `D-20260815-KTXJSHGZ`).
The discovery journal is the authoritative source of truth for discovery IDs.

- The field is validated against the journal format pattern `^D-[0-9]{8}-[0-9A-Za-z]+$`.
- Section headers in `digests/DISCOVERIES.md` define valid discovery IDs.
- Malformed discovery IDs (including bare ULIDs) are rejected at schema validation time.
- An unknown `discovery_id` is refused immediately before any database write:
  ```
  Discovery '<discovery_id>' not found in digests/DISCOVERIES.md
  ```
---

## 5. Catalog Schema and SQL Views

### Table DDL (`sql/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS verdicts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    discovery_id text NOT NULL,
    status text NOT NULL,
    "by" text NOT NULL,
    "at" timestamptz NOT NULL,
    note text,
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS verdicts_discovery_idx ON verdicts (discovery_id);
CREATE INDEX IF NOT EXISTS verdicts_at_idx ON verdicts ("at" DESC);
CREATE INDEX IF NOT EXISTS verdicts_status_idx ON verdicts (status);
```

### Views (`sql/verdicts.sql` & `sql/schema.sql`)

- **`v_current_verdicts`**: Resolves the single latest verdict per discovery:
  ```sql
  CREATE OR REPLACE VIEW v_current_verdicts AS
  WITH ranked AS (
      SELECT
          discovery_id,
          status,
          "by",
          "at",
          note,
          row_number() OVER (
              PARTITION BY discovery_id
              ORDER BY "at" DESC
          ) AS ranking
      FROM verdicts
  )
  SELECT
      discovery_id,
      status,
      "by",
      "at",
      note
  FROM ranked
  WHERE ranking = 1
  ORDER BY "at" DESC, discovery_id;
  ```

- **`v_verdicts_history`**: Returns the complete chronological history of all decisions, oldest first:
  ```sql
  CREATE OR REPLACE VIEW v_verdicts_history AS
  SELECT
      discovery_id,
      status,
      "by",
      "at",
      note
  FROM verdicts
  ORDER BY discovery_id, "at" ASC;
  ```

---

## 6. CLI Usage

### Record a Verdict

```bash
# Record an accepted finding
uv run evallab verdict D-20260815-KTXJSHGZ accepted --by "Peter Makhnatch" --note "Verified against logs"

# Re-decide as needs_evidence (appends a new row; prior row preserved)
uv run evallab verdict D-20260815-KTXJSHGZ needs_evidence --by "Peter Makhnatch" --note "More sample trials needed"
```
### List Current Verdicts

```bash
# List all latest verdicts
uv run evallab verdict list

# Filter by status
uv run evallab verdict list --status accepted

# Machine-readable JSON output
uv run evallab verdict list --json
```

### Inspect Decision History

```bash
# View all historical decisions for one discovery
uv run evallab verdict history D-20260815-KTXJSHGZ

# History in JSON format
uv run evallab verdict history D-20260815-KTXJSHGZ --json
```
