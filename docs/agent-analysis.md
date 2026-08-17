---
status: living
audience:
  - analyst
  - operator
  - builder
---

# Agent Analysis

Durable agent analysis with stored reasoning trajectories for the Harbor evaluation corpus.

## The Problem

When an evaluation trial finishes, an agent or human analyst can inspect what occurred and why it
failed or succeeded. Previously, ad-hoc analyses were ephemeral or stored in unindexed stub files,
losing durable findings and providing zero audit trail of *how* a conclusion was reached.

The analyst pipeline solves this by capturing two distinct durable artifacts:
1. **The conclusion (`AnalysisRecord`)**: The structured verdict, failure category, confidence claim,
   and concrete evidence citations.
2. **The analyst's trajectory**: The exact sequence of steps taken during analysis—what metadata was
   queried, what trajectory files were read, what hypotheses were evaluated, and how the verdict was formed.

## Hard Budget Rule and Token Safety

- **Default Execution**: Running `evallab analyst run <trial_id>` invokes a deterministic, offline
  `StubAnalyzer`. It performs zero network calls and spends zero provider tokens.
- **Explicit Model Opt-In**: Invoking an external LLM requires passing `--model <selector>` (e.g.
  `--model gpt-4o`). Model dispatch is token-gated and documented as consuming provider spend.
  Calling a model backend without explicit selector or credentials raises `ModelProviderRefusedError`.

## Data Contracts and Record Shape

All analysis records conform to the golden contract `AnalysisRecord` in `src/evallab/schemas.py`:

```python
class AnalysisRecord(ContractModel):
    schema_version: Literal[1] = 1
    analysis_id: str          # ULID primary key
    trial_id: str             # ULID join spine to trial
    rubric_digest: str        # sha256: of evaluation rubric applied
    model: str                # Identifier of judge or 'stub'
    category: str             # Taxonomy failure mode or capability
    evidence: list[EvidenceCitation]  # Mandatory citations (path + optional step)
    confidence: ConfidenceClaim       # Qualitative label, n, interval, provenance digest
```

### Evidence Requirement
Every conclusion **must** cite at least one concrete piece of evidence (file path or trajectory step).
An analysis returning an empty evidence list is **rejected** and never written to disk, preventing
unevidenced assertions from appearing as durable findings.

### Lineage and Disagreement Preservation
- **Durable Storage**: Conclusions are written to `research/analysis/<analysis_id>.json`. Each record
  embeds an `inputs: [{path, digest}]` block registering the raw trial files and trajectories examined.
  Lineage walking via `evallab lineage research/analysis/<analysis_id>.json` traces back to Zone 1 evidence.
- **Disagreement Preservation**: Re-running analysis on the same trial generates a new unique ULID
  `analysis_id`. Prior records are never overwritten; differing conclusions for the same trial persist
  side-by-side to highlight analyst disagreement.

## Stored Analyst Trajectories

Beside each conclusion record, the analyst's ordered reasoning steps are stored at
`research/analysis/<analysis_id>.trajectory.json`:

```json
{
  "analysis_id": "01M08R8395BQRAWMXRXS541VB7",
  "trial_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "created_at": "2026-08-17T20:55:00Z",
  "steps": [
    {
      "step_id": 0,
      "source": "attacher",
      "timestamp": "2026-08-17T20:55:00Z",
      "message": "Attached unified surface and resolved trial '01ARZ3NDEKTSV4RRFFQ69G5FAV'"
    },
    {
      "step_id": 1,
      "source": "reader",
      "timestamp": "2026-08-17T20:55:01Z",
      "message": "Loaded 12 trajectory steps from raw artifacts"
    },
    {
      "step_id": 2,
      "source": "analyzer",
      "timestamp": "2026-08-17T20:55:02Z",
      "message": "Executing analysis with StubAnalyzer"
    }
  ]
}
```

## Unified Attach Surface and SQL Views

Analysis records and trajectories are projected into Parquet under `derived/parquet/` and exposed via
`sql/analyst.sql`.

### Views Registered
1. `v_analysis_records`: Stored conclusions with `analysis_id`, `trial_id`, `model`, `category`,
   `evidence_count`, `confidence_level`, and timestamps.
2. `v_analyst_trajectories`: Stored reasoning steps with `analysis_id`, `step_id`, `source`,
   `timestamp`, and `message`.
3. `v_analysis_with_trajectory`: Joins conclusions directly to their reasoning steps on `analysis_id`.

### Clean DuckDB Session Resolution
`sql/analyst.sql` includes schema fallback tables allowing clean in-memory execution with zero
pre-created tables:

```bash
duckdb -c ".read sql/analyst.sql" -c "SELECT * FROM v_analysis_records"
```

### Querying via Attach Surface
Query across Z1/Z2/Z3/Z4 with `evallab db attach`:

```bash
uv run evallab db attach --query "SELECT analysis_id, category, step_id, step_message FROM v_analysis_with_trajectory LIMIT 10"
```

## CLI Usage

```bash
# Run deterministic stub analysis on a trial
uv run evallab analyst run trial_01

# Run model analysis (spends tokens; requires credentials)
uv run evallab analyst run trial_01 --model gpt-4o

# List stored analysis conclusions
uv run evallab analyst list
uv run evallab analyst list --trial 01ARZ3NDEKTSV4RRFFQ69G5FAV

# Show conclusion and reasoning trajectory
uv run evallab analyst show 01M08R8395BQRAWMXRXS541VB7
uv run evallab analyst show 01M08R8395BQRAWMXRXS541VB7 --json

# Walk lineage back to raw evidence
uv run evallab lineage research/analysis/01M08R8395BQRAWMXRXS541VB7.json
```
