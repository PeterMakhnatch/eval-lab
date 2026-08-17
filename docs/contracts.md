---
status: living
audience:
  - builder
  - analyst
---

# Platform Contracts (E00)

This module (`src/evallab/contracts.py`) defines the five §2.1 entities that had no prior model. They are the frozen interface spine that all later epics bind to (T2).

## Entities implemented here

| Entity | Key | Core fields from §2.1 | Invariants enforced |
|---|---|---|---|
| `Suite` | (name, version) | member TaskVersion refs[], frozen_at | `frozen_at` set ⇒ instance rejects mutation via `__setattr__` guard |
| `AnalysisRecord` | analysis_id | trial_id, rubric_digest, model, category, evidence[{path,step}], confidence | ULID ids, sha256: digests, ConfidenceClaim carries n/interval/provenance (T4) |
| `ObservationRecord` | trial_id | template_version, factual fields per OBSERVATORY TEMPLATE | ULID trial_id; fields exactly from `research/observations/TEMPLATE.md` |
| `CalibrationRecord` | calib_id | judge_model, rubric_digest, corpus_digest, per-criterion agreement, date | ULID, sha256: digests, CriterionAgreement (agreements/total/rate) not bare float (T4) |
| `Verdict` | (discovery_id) | status ∈ {accepted, rejected, needs_evidence, pending}, by, at, note | status literal set only; ULID discovery_id |

## Other §2.1 entities (already modelled — do not re-declare)

| Entity | Location | Key |
|---|---|---|
| AgentProfile | `src/evallab/profiles.py` | agent_name |
| TaskVersion | `src/evallab/registry.py` | (task_ref, version) |
| Job | `src/evallab/explorer.py` | job_id |
| Trial | `src/evallab/atif.py` | trial_id |
| Trajectory | `src/evallab/atif.py` | trial_id |
| CraftRecord | `src/evallab/craft.py` | (task_ref, facets_schema_version) |
| Proposal | `src/evallab/authoring.py` | proposal_id |
| Lesson | `src/evallab/lessons.py` | lesson_id |
| ExperimentSpec | `src/evallab/schemas.py` | spec_id |
| QueueEvent | `src/evallab/schemas.py` | event_id |

## Golden schema freeze

Every model serialises its `model_json_schema()` to `tests/fixtures/contracts/<Model>.json`.

A test (`tests/test_contracts.py:test_golden_schemas_match_live`) asserts byte-for-byte equality. Adding, renaming, retyping, or reordering any field fails CI with a diff — the explicit mitigation for schema churn risk named in platform-architecture §11.

## Intentional regeneration (only deliberate changes)

```bash
uv run python -c '
import json
from pathlib import Path
from evallab.contracts import Suite, AnalysisRecord, ObservationRecord, CalibrationRecord, Verdict
fixtures = Path("tests/fixtures/contracts")
for Model in [Suite, AnalysisRecord, ObservationRecord, CalibrationRecord, Verdict]:
    schema = Model.model_json_schema()
    out = fixtures / f"{Model.__name__}.json"
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"updated {out}")
'
```

Run this, review the diff, then commit. Accidental edits are loud; deliberate ones are one command.

## Round-trip and rejection tests

`test_roundtrip_all_models` proves `model_dump` → `model_validate` identity.

Rejection tests prove every validator actually fires:
- non-ULID id
- unprefixed / malformed digest
- status outside the literal set
- mutation of a `Suite` with `frozen_at` set

A validator that never rejects is not a validator; these tests cover the contract.

## Usage

```python
from evallab.contracts import Suite, AnalysisRecord, ...
```

All models inherit `ContractModel` (extra="forbid", strict). schema_version: Literal[1] on every record.
