---
status: living
audience:
  - builder
  - operator
---

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

3. **Promoted Control Evidence**:
   Every registered task must cite durable, promoted Oracle (exact reward = 1.0) and Nop
   (exact reward = 0.0) evidence files by repository-relative path, cryptographic SHA-256
   digest, and UTC observation timestamp. Invented job names or unpromoted runs fail closed.

4. **Canonical Resolution**:
   Experiment specs claiming `registered/<task_id>` are resolved into canonical
   `task_path`, `task_version`, `verifier_digest`, `package_digest`, and limits. Omitted
   `task_path` automatically resolves to the canonical registry path. Redirection away from
   the canonical path raises `TaskPathRedirectionError`.

5. **Allowed Uses Enforcement**:
   Tasks declare permitted uses (`measurement`, `training`, `canary`, `heldout`, `foundry-seed`).
   Training-only tasks cannot be executed for measurement. The researcher loop preflights
   the explicit registry before invoking any LLM agent.

6. **Human-Gated Registration**:
   Promoting a task from `candidate` to `registered` requires human approval
   (`approved_by` and `approved_at`) plus verified control evidence. Peter Makhnatch owns all
   registration decisions.

7. **Independent Canary Policy**:
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
      "job_name": "event-summary-oracle-evidence",
      "reward": 1.0,
      "evidence_path": "research/evidence/runs/event-summary-oracle-evidence/result.json",
      "evidence_digest": "sha256:94008ac5b3559dbade582a0ad3373a5f56957438f5621ce72fe77e94ec28229e",
      "observed_at": "2026-08-13T20:33:44.112624Z"
    },
    "nop": {
      "job_name": "event-summary-nop-evidence",
      "reward": 0.0,
      "evidence_path": "research/evidence/runs/event-summary-nop-evidence/result.json",
      "evidence_digest": "sha256:bf7787daa7360fed39fd975f2adb03025a6d157d8fd41fb222e1d55f34dfb1a8",
      "observed_at": "2026-08-13T20:33:54.832213Z"
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
| `task_path` | `str` | Repo-relative path | Must exist, must not escape repo, must have task.toml, instructions, environment, verifier |
| `digests` | `TaskDigests` | Package & component SHA-256 | Must match on-disk file bytes |
| `source_ref` | `str \| None` | Upstream commit/ref | Pinned immutable SHA or release tag if `provenance_zone == "01-external"` (no floating branches) |
| `license` | `str \| None` | Declared license | Required if `provenance_zone == "01-external"` |
| `provenance_zone` | `Literal` | Provenance zone (`01-external` ... `04-curated`) | Defined per docs/data-architecture.md |
| `is_synthetic` | `bool` | Whether generated or adapted | Boolean flag |
| `limits` | `TaskLimits` | Execution constraints | `timeout_seconds` between 1 and 21600 |
| `control_evidence` | `TaskControlEvidence` | Oracle and Nop verification runs | Oracle reward must be exactly 1.0, Nop reward exactly 0.0, evidence files must exist, match digests, and parse cleanly |
| `state` | `Literal` | `candidate`, `registered`, or `retired` | Distinct states |
| `allowed_uses` | `list[Literal]` | `canary`, `measurement`, `heldout`, `foundry-seed`, `training` | Unique items, min length 1 |
| `approved_by` | `str \| None` | Approver identity | Required if `state == "registered"` |
| `approved_at` | `datetime \| None` | Approval timestamp (UTC) | Required if `state == "registered"` |

---

## 3. CLI Interface

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
2. Control evidence exists, matches SHA-256 digest, and proves exact Oracle=1.0 and Nop=0.0.
3. External records have declared licenses and pinned revisions.
4. No queue proposals claim unregistered or candidate tasks (`false_registered_claim`).
5. No queue proposals attempt `task_path` redirection or version/verifier mismatches.
6. Malformed JSON in queue specs or registry files is reported as error findings without swallowing parse errors.
7. Curated cards that are pointer-only documentation are identified.
