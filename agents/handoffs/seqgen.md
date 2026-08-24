Status: review-wanted
Last: SEQGEN v0 deterministic sequence-first task generator implemented with greedy bigram coverage selection, comprehensive unit test suite (a-h), and committed initial batch `seqgen-v0`
Next: PR review and orchestrator smoke evaluation (oracle / nop controls)
Blockers: none

# SEQGEN v0 — Sequence-First Synthetic Harbor Task Generator

## Outcome & Summary

SEQGEN v0 (`evallab.seqgen`, transform `seqgen@0.1.0`) is a clean, sequence-first synthetic task generator implementing the published TASTE paradigm from scratch without external dependencies or third-party code. It generates valid tool sequences over a deterministic JSONL record-pipeline domain, selects sequences greedily for maximal op-bigram coverage, and instantiates self-contained Harbor task packages certified by `task_workbench`.

## Domain & Tool Schema

- **Domain:** Synthetic tabular records in `/app/data/orders.jsonl` (40–60 rows per task, seeded PRNG) with fields `id` (int), `region` (str in `{north, south, east, west}`), `status` (str in `{shipped, pending, cancelled, returned}`), `amount` (int 5..500), and `day` (int 1..28).
- **Single Source of Truth (`RP_SOURCE`):** A self-contained, stdlib-only Python script (`/app/bin/rp`) implementing 7 pure operations + CLI subcommands + canonical writer. The generator's simulator dynamically loads `RP_SOURCE` via `exec()` to ensure zero divergence between simulation and runtime behavior.
- **Operations:**
  1. `filter_eq(field, value)` — filter rows by string equality (non-empty output).
  2. `filter_ge(field, value)` — filter rows by integer threshold (strictly reducing, non-empty output).
  3. `select(fields)` — project rows to proper subset of current fields ($\ge 2$ fields).
  4. `sort_by(field, order)` — stable sort ascending or descending.
  5. `dedupe_by(field)` — keep first occurrence per key value (strictly reducing).
  6. `head(n)` — retain first $n \in \{3, 5, 10\}$ rows ($n < \text{len}(\text{rows})$).
  7. `group_sum(group_field, value_field)` — aggregate into `[group_field, "total_" + value_field]` sorted ascending.
  - Terminal: `write` — canonical compact JSONL serialization with sorted keys and trailing newline.
- **Sequence Constraints:** 3–6 operations before terminal `write`; $\ge 2$ distinct op types; all typed preconditions verified along simulation trajectory; no consecutive duplicate `(op, args)`.

## Coverage Selection Algorithm

- **Bigram Reachability:** Statically enumerated across $7 \times 7$ op pairs plus 7 terminal transitions. 1 pair is statically impossible (`group_sum` $\to$ `select`, as `group_sum` produces 2 fields while `select` requires proper subset $\ge 2$), yielding $48 + 7 = 55$ reachable bigrams.
- **Greedy Coverage Selection:** From a candidate pool (default 40), sequences are selected iteratively to maximize newly covered op bigrams, tiebreaking by newly covered unigrams and candidate pool index.
- **Batch Coverage (seed=7, count=3, pool=40):**
  - Unigram coverage: 7/7 (100%)
  - Bigram coverage: 15/55 reachable (27.3%)

## Package Structure & Standards

Generated tasks under `library/synthetic/<batch>/<slug>` mirror `library/tasks/event-summary` conventions:
- `task.toml`: Harbor 1.4 schema, separate verifier environment with `network_mode = "no-network"`, pinned base image `python:3.13-slim-bookworm@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251`.
- `instruction.md`: Declarative English goal descriptions (e.g. folding consecutive filters into compound "where X and Y" clauses, folding sort_by + head into top-K clauses). No solution leak.
- `environment/`: Dockerfile, `orders.jsonl`, executable `rp` tool (`0o755`).
- `solution/solve.sh`: Executable shell script piping `/app/bin/rp` steps (`0o755`).
- `tests/`: Dockerfile, `test.sh` (`0o755`), `verify.py` reporting checks, CTRF, and reward payloads.
- `tests/fixtures/`: Trusted `orders.jsonl` and `expected.jsonl`.
- `workbench/adversarial/`: `empty-output.sh`, `copy-input.sh`, `plausible-wrong.sh` (all executable `0o755`).
- `generation.json`: Generator metadata, parameter seeds, step sequence, digests, row counts.
- `provenance.json`: Validated against `evallab.schemas.ProvenanceMetadata` with content-addressed `material_digest` tree manifest and parent digests (RP_SOURCE and DOMAIN_SPEC).

## Evidence & Verification

All focused tests in `tests/test_seqgen.py` passed in <1s with 0 errors:
```
============================== 9 passed in 0.66s ===============================
```

### Covered Test Matrix:
1. `test_a_determinism`: Byte-identical directory tree and identical sha256 digests across repeat runs with identical seed/args/now.
2. `test_b_validity`: Replaying recorded sequences through step-by-step precondition enumerator passes all checks and yields non-empty output matching expected rows.
3. `test_c_simulator_rp_equivalence`: Executing `solve.sh` commands as real subprocesses against `orders.jsonl` yields byte-identical output to `expected.jsonl`.
4. `test_d_workbench_static_acceptance`: `task_workbench.inspect_candidate` passes with `static_passed=True` and 0 errors/warnings.
5. `test_e_leakage_prevention`: No `solve.sh` line appears in `instruction.md`; `environment/` contains no verifier/golden files; `expected.jsonl` exists only in `tests/fixtures/`.
6. `test_f_adversarial_wrongness`: `plausible-wrong.sh` outputs valid JSONL guaranteed not to match expected output.
7. `test_g_coverage_correctness`: `BATCH.json` bigram set matches recomputed sequence bigrams; first selection pick is greedy-optimal.
8. `test_h_provenance_integrity`: `provenance.json` passes `ProvenanceMetadata` validation and `material_digest` matches tree manifest sha256.
9. `test_directory_immutability`: `generate_batch` refuses to overwrite non-empty directories.

Ruff linting:
```bash
uv run ruff check src/evallab/seqgen.py tests/test_seqgen.py library/synthetic
# All checks passed!
```

## Generated Batch Artifacts

Committed batch `library/synthetic/seqgen-v0` generated via:
`uv run python -m evallab.seqgen --seed 7 --count 3 --pool 40 --out library/synthetic/seqgen-v0`
- `library/synthetic/seqgen-v0/BATCH.json`
- `library/synthetic/seqgen-v0/seqgen-s7-000` (length 5 ops, difficulty: medium)
- `library/synthetic/seqgen-v0/seqgen-s7-001` (length 6 ops, difficulty: medium)
- `library/synthetic/seqgen-v0/seqgen-s7-002` (length 4 ops, difficulty: easy)

## Proposed Board Row

| Mission | Lane | Role Branch | Status | Outcome |
|---|---|---|---|---|
| M053 | Tasks | role/seqgen | review-wanted | SEQGEN v0 generator + unit tests + initial batch committed |
