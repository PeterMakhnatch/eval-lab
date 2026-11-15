# Task health card: terminal-bench-lite-verified@f8f4fc8

Measured 2026-11-15 by Eval Lab. Every row is an executed reward from a fresh container, not a reading of the source code.

## Method

| Cell | What runs | What it shows |
|---|---|---|
| reference | the task's own `solution/solve.sh` | the task is solvable as shipped |
| nop | the same task, no work done | the grader does not pay for nothing |

One command reproduces the table: `evallab qa corpus <corpus-root> --out <dir> --resume`.
Cost: $0.32 billed Modal for 190 cells (measured 2026-11-15), no model calls.

## Counts

| Class | Tasks | Meaning |
|---|---:|---|
| `control_paid` | 0 | pays for no work (nop > 0) |
| `reference_below_one` | 9 | shipped reference does not reach 1 |
| `reference_unmeasured` | 10 | reference could not be measured |
| `cannot_run` | 1 | neither cell ran |
| `no_reference` | 26 | no reference shipped (placeholder) |
| `clean` | 49 | reference 1, nop 0 |
| **total** | **95** | |

## Overlap with the upstream defect list

| Reading | Tasks | What it means |
|---|---:|---|
| Upstream lists it, and the executed reference fails or cannot run | 7 | their source reading is confirmed by a measurement |
| Upstream lists it, and it ships no reference | 1 | listed, and also has no reference solution |
| Upstream lists it, and it measures clean | 1 | not reproduced as a solvability defect |
| Upstream does not list it, and it ships no reference | 25 | the largest unclaimed gap |
| Upstream does not list it, and the executed reference fails or cannot run | 13 | new executed findings outside their list |

## Rows

| Task | Reference | nop | Class | Upstream |
|---|---|---|---|---|
| `bash-log-processor-fix` | 0.605128 (executed) | 0 (executed) | `reference_below_one` | listed, still present |
| `bloom-filter-cache-penetration-prevention` | 0 (executed) | 0 (executed) | `reference_below_one` | listed, still present |
| `network-log-normalization` | 0 (executed) | 0 (executed) | `reference_below_one` | listed, still present |
| `symlink-chain-traversal` | 0 (executed) | 0 (executed) | `reference_below_one` | listed, still present |
| `acl-permissions-inheritance` | 0 (executed) | 0 (executed) | `reference_below_one` | not listed |
| `corrupted-filesystem-recovery` | 0 (executed) | 0 (executed) | `reference_below_one` | not listed |
| `protein-sequence` | 0 (executed) | 0 (executed) | `reference_below_one` | not listed |
| `security-breach-incident-response` | 0.75 (executed) | 0 (executed) | `reference_below_one` | not listed |
| `security-incident-log-analysis` | 0.925 (executed) | 0 (executed) | `reference_below_one` | not listed |
| `breast-cancer-mlflow` | n/a (failed) | 0 (executed) | `reference_unmeasured` | listed, still present |
| `okhttp-trailers-crash` | n/a (failed) | 0 (executed) | `reference_unmeasured` | listed, still present |
| `amuse-install` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `cpp-daemon-sighup-segfault` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `iot-device-registration-server` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `pandas-etl` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `pdf-table-parsing` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `reverse-engineer-stack-vm` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `smiles-data-lab` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `sql-injection-forensics` | n/a (failed) | 0 (executed) | `reference_unmeasured` | not listed |
| `etl_checkpoint_resume_bug` | n/a (failed) | n/a (failed) | `cannot_run` | listed, still present |
| `floor-plan-geometry` | n/a (not_run) | n/a (not_run) | `no_reference` | listed, still present |
| `basic-message-queue` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `battery-charging-optimization` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `book-portfolio-analysis` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `bracket-sequence-restoration` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `competitive-programming-solver` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `game-of-stones` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `grid-pathfinding` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `grpc-plant-position-server` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `html-index-analysis` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `image-tile-identification` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `iris-dataset-classification` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `legal-summary-extraction` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `multi-server-configuration` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `pandas-numpy-data-analysis` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `parking-lot-pathfinding` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `permutation-construction-100k` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `playing-card-recognition` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `prediction-model-evaluation` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `publisher-market-analysis` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `publisher-market-analysis-v2` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `sakila-sqlite-queries` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `server-log-analysis` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `simple-database-query-tool` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `sympy-bug-fix` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `word-derangement-mapping` | n/a (not_run) | n/a (not_run) | `no_reference` | not listed |
| `california-housing-api` | 1 (executed) | 0 (executed) | `clean` | listed, still present |
| `anomaly-detection-ranking` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `api-endpoint-permission-canonicalizer` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `application-debug` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `auth_token_race_condition` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `bandit-delayed-feedback` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `broken-python` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `build-merkle-tree-cli-sha512` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `build-system-task-ordering` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `chained-forensic-extraction_20260101_011957` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `container-registry-optimization` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `convolutional-layers` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `cosign-keyless-signing` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `csv-json-jsonl-merger` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `db-migration-local-storage` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `distributed-test-execution-scheduler` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `ekf-localization` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `fix-js-network-controller` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `fix_async_worker_queue` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `hydra-debug-slurm-mode` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `industrial-kiln-controller` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `jq-data-processing` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `jsonl-aggregator` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `log-summary` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `malicious-package-forensics` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `maven-slf4j-conflict` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `mech-system` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `mlflow-register` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `monorepo-changelog-cli` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `mtls-cert-rotation` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `multi-labeller` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `neural-architecture-search-final` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `neutron-submission` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `python-api-rate-limit` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `raft-log-repair-concurrent-access` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `react-typescript-debugg` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `reproducibility-and-envsetup` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `rsa-jwt-token-api-redis-blacklist` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `sales-data-csv-analysis` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `scan-linux-persistence-artifacts` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `schedule-vacation` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `service-deployment-wave-planner` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `sign-vector-game` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `submission_a63937a5_20251224_152124` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `supply-chain-fulfillment` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `task-xxe-exploit` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `todos-api` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `token-auth-websocket` | 1 (executed) | 0 (executed) | `clean` | not listed |
| `vimscript-vim-quine` | 1 (executed) | 0 (executed) | `clean` | not listed |

## Limits

- A row measures one execution of one cell. A task that fails here fails as shipped; a task that passes may still be wrong for other reasons (a missing requirement, an exposed key, a check that accepts a wrong answer).
- `no_reference` says only that no reference solution shipped. It is not a claim about the grader.
- Where the upstream document already lists a task, this card adds the executed verdict; the credit for finding it is theirs.
- This card measures solvability and nop payoff. It does not test contamination, exposed answer keys, or whether a grader accepts a wrong answer; a clean row can still be a contaminated task.
