# Task registry and admission trust boundary

Status: normative design and operational reference. Owner: REGISTER. Date: 2026-08-15.

This document defines how tasks enter the evaluable registry of `eval-lab`,
how the admission trust boundary is enforced across the queue and executor, and
how task integrity is audited.

---

## 1. Principles

1. **Explicit-Only Admission**:
   Filesystem location, the presence of `task.toml`, a curated card in `library/curated/`,
   or inclusion in `policy/canary-suite.yaml` **never implies registration**. A task is
   registered if and only if an explicit, valid, human-approved JSON record exists in
   `library/registry/<task_id>.json`.

2. **Immutable Integrity Binding**:
   Every registered task record binds the exact cryptographic digests (SHA-256) of its
   `task.toml`, instructions, environment, verifier, and full package directory. Any byte
   modification on disk invalidates admission and halts dispatch.

3. **No Redirection**:
   An experiment spec claiming `registered/<task_id>` cannot redirect execution to an
   arbitrary `task_path` or alternative version.

4. **Human-Gated Registration**:
   Promoting a task from `candidate` to `registered` requires human approval
   (`approved_by` and `approved_at`) plus verified control evidence (Oracle reward = 1.0,
   Nop reward = 0.0). Peter Makhnatch owns all registration decisions.

5. **Independent Canary Policy**:
   Canaries operate under `policy/canary-suite.yaml` to ensure repository health and
   prevent chicken-and-egg bootstrap cycles. Canaries do not require registry admission.

---

## 2. Canonical Registry Record Schema

Each registered or candidate task is stored as a JSON file in `library/registry/<task_id>.json`
conforming to `evallab.schemas.TaskRegistryRecord`:

```json
{
  "schema_version": 1,
  "task_id": "event-summary",
  "version": "1.0.0",
  "task_path": "library/tasks/event-summary",
  "digests": {
    "task_toml": "sha256:7f4c28f114c004d46e3381a8b98165d75cb1be309bf1ca66b26ec588e7b16524",
    "instruction": "sha256:919eb452bba94ebaa8d56b063ee3ce5562771d9d95be09c7333557e4ca67fe72",
    "environment": "sha256:1fa50cefa37c5ea2c6b4fa7ee4313f89f7fc9e51ea460f4eb8488e04012be407",
    "verifier": "sha256:495bc2e2a7b8e1a1a5b81a4a49c950a7f1a3089d81d2f78b1735165b40cfeb52",
    "package": "sha256:bee722a27298eb06f5010b18da7c27295b1ff6236aa03ce58c5e5b1df4d0d61d"
  },
  "source_uri": "local/event-summary@1.0.0",
  "source_ref": "main",
  "license": "MIT",
  "provenance_zone": "02-local-evidence",
  "is_synthetic": false,
  "limits": {
    "timeout_seconds": 1800,
    "max_memory_mb": 512,
    "max_cpus": 1.0
  },
  "control_evidence": {
    "oracle": {
      "job_name": "event-summary-oracle-control",
      "reward": 1.0,
      "evidence_path": "research/evidence/runs/event-summary-oracle/result.json",
      "observed_at": "2026-08-14T12:00:00Z"
    },
    "nop": {
      "job_name": "event-summary-nop-control",
      "reward": 0.0,
      "evidence_path": "research/evidence/runs/event-summary-nop/result.json",
      "observed_at": "2026-08-14T12:05:00Z"
    }
  },
  "state": "registered",
  "allowed_uses": ["measurement", "training", "canary"],
  "approved_by": "Peter Makhnatch",
  "approved_at": "2026-08-15T12:00:00Z"
}
```

### Fields & Validation Contract

| Field | Type | Description | Invariants |
|---|---|---|---|
| `schema_version` | `1` | Schema version | Must be `1` |
| `task_id` | `str` | Stable task slug | 3–80 lowercase alphanumeric characters / hyphens |
| `version` | `str` | Task version string | Non-empty |
| `task_path` | `str` | Repo-relative path | Must exist, must not escape repo |
| `digests` | `TaskDigests` | Package & component SHA-256 | Must match on-disk file bytes |
| `provenance_zone` | `Literal` | Provenance zone (`01-external` ... `04-curated`) | Defined per docs/data-architecture.md |
| `is_synthetic` | `bool` | Whether generated or adapted | Boolean flag |
| `limits` | `TaskLimits` | Execution constraints | `timeout_seconds` between 1 and 21600 |
| `control_evidence` | `TaskControlEvidence` | Oracle and Nop verification runs | Oracle reward must be 1.0, Nop reward 0.0 for registered state |
| `state` | `Literal` | `candidate`, `registered`, or `retired` | Distinct states |
| `allowed_uses` | `list[Literal]` | `canary`, `measurement`, `heldout`, `foundry-seed`, `training` | Unique items, min length 1 |
| `approved_by` | `str \| None` | Approver identity | Required if `state == "registered"` |
| `approved_at` | `datetime \| None` | Approval timestamp (UTC) | Required if `state == "registered"` |

---

## 3. Task Admission Lifecycle

```text
 ┌───────────────────────────────────────────────────────────┐
 │                   1. TASK PACKAGE AUTHORING               │
 │ • Write task.toml, environment/, tests/, solution/        │
 │ • Placed in library/tasks/, benchmarks/, or adapters/     │
 └─────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
 ┌───────────────────────────────────────────────────────────┐
 │                   2. CANDIDATE REGISTRATION               │
 │ • Generate Candidate JSON record in library/registry/     │
 │ • state: "candidate"                                      │
 │ • Compute package & component digests                     │
 └─────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
 ┌───────────────────────────────────────────────────────────┐
 │                   3. CONTROL CERTIFICATION                │
 │ • Execute Oracle control -> must score reward = 1.0       │
 │ • Execute Nop control    -> must score reward = 0.0       │
 │ • Record evidence paths and job names                     │
 └─────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
 ┌───────────────────────────────────────────────────────────┐
 │                   4. HUMAN REVIEW & APPROVAL              │
 │ • Peter reviews review packet & candidate record          │
 │ • approved_by: "Peter Makhnatch", approved_at: <now>      │
 │ • state promoted to: "registered"                         │
 └─────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
 ┌───────────────────────────────────────────────────────────┐
 │                   5. ADMISSION FOR RESEARCH               │
 │ • ResearcherLoop proposes follow-ups against task         │
 │ • PolicyGate & Executor admit registered/* specs          │
 └───────────────────────────────────────────────────────────┘
```

---

## 4. CLI Interface

### List Tasks
```bash
# List all registered tasks
uv run evallab registry list

# List candidate tasks
uv run evallab registry list --state candidate

# Output JSON
uv run evallab registry list --json
```

### Audit Registry
```bash
# Run comprehensive read-only audit
uv run evallab registry audit

# Output JSON audit report
uv run evallab registry audit --json
```

The audit verifies:
1. Every registered record exists on disk with matching file bytes and verifiers.
2. Control evidence is valid (Oracle=1.0, Nop=0.0).
3. No queue proposals claim unregistered or candidate tasks (`false_registered_claim`).
4. No specs attempt `task_path` redirection.
5. Curated cards that are pointer-only documentation are identified.
