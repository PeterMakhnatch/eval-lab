# Task lint against executed health classes (HAR-454)

Date: 2026-11-15. Cost: **$0**. **No task was executed**.
This join runs the read-only static `evallab tasks lint` code on a corpus clone and the
committed executed health card. It uses no Docker, no network, no model calls, and no Modal.

## The answer

The health card has 95 tasks: 49 `clean` and 46 not clean.
Task lint reports **314 findings** on the same 95 task packages.
**No lint code flags only clean tasks.**
Two codes flag only non-clean tasks, and they are narrow:

| Code | Tasks | Classes |
|---|---:|---|
| `sources-parse` | 1 | `reference_below_one` only |
| `unpinned-clone` | 2 | `reference_unmeasured` and `no_reference` |

Two codes fire on all 95 tasks, so they separate no executed class:
`unpinned-image` and `verifier-isolation`.
The clean class has the highest mean finding count, 3.96, because it has no `sources-parse`
or `unpinned-clone` row and the same universal codes as every other task.
This table gives no reason to expect the current lint set to find executed failures.
A next lint investment must add checks that fire on failing tasks and stay quiet on clean
tasks; the codes today do the opposite or nothing.

## Data and join

Inputs:

- Card: [`research/audits/corpus-health-tblite-verified-2026-11-15.json`](../../audits/corpus-health-tblite-verified-2026-11-15.json), 95 `rows[]` with `task` and `class`.
- Corpus: `git clone https://github.com/huggingface/terminal-bench-lite-verified /tmp/tblv` at commit `f8f4fc8f738d4819eba8eb88bb5033c58e7ec172`.

The join key is the card task name. It must equal a task directory name in the clone,
and that directory must hold `task.toml`.
The check found no mismatch: 95 of 95 card tasks resolve, the card has no duplicate task,
and the clone has exactly 95 task packages. The clone's own `tests/` directory is not a
task package and is not in the card.

## Method

The builder imports `evallab.task_lint.lint_task` and calls it on each joined task directory.
It does not run the task, execute a container, or write into the clone.
The CLI form is `uv run evallab tasks lint /tmp/tblv --json`; it reads the same functions.

For each task the builder records:

- `task`: the exact card task name and directory name.
- `executed_class`: the card class (`clean`, `reference_below_one`, `reference_unmeasured`, `no_reference`, `cannot_run`).
- `lint_codes`: the unique finding rules, sorted, joined by `;`; empty for no findings.
- `finding_count`: every finding, including repeated rules.

Rows are in task-name order. The CSV has 95 data rows and 96 lines, with LF endings.
Reproduce from the repository worktree:

```bash
git clone https://github.com/huggingface/terminal-bench-lite-verified /tmp/tblv
git -C /tmp/tblv checkout f8f4fc8
uv run python research/experiments/task-lint-vs-executed-health/build.py
uv run python research/experiments/task-lint-vs-executed-health/build.py --check
```

`--check` compares exact bytes and writes nothing. It exits 0 when the CSV is current
and 1 on drift or a missing CSV. `TBLV_CORPUS_ROOT` and `TBLV_HEALTH_CARD` override the
default roots; `--out-dir` writes elsewhere.

## Lint codes by executed class

Cells count flagged tasks. A task counts once per code, even when the code repeats.

| Lint code | `clean` | `reference_below_one` | `reference_unmeasured` | `no_reference` | `cannot_run` | All |
|---|---:|---:|---:|---:|---:|---:|
| `sources-parse` | 0 | 1 | 0 | 0 | 0 | 1 |
| `unpinned-clone` | 0 | 0 | 1 | 1 | 0 | 2 |
| `unpinned-image` | 49 | 9 | 10 | 26 | 1 | 95 |
| `unpinned-python-dependency` | 4 | 0 | 3 | 3 | 0 | 10 |
| `verifier-isolation` | 49 | 9 | 10 | 26 | 1 | 95 |
| `verifier-network-download` | 44 | 2 | 5 | 0 | 0 | 51 |
| `verifier-runtime-install` | 46 | 2 | 5 | 0 | 0 | 53 |

Group membership, as the card asks:

- Codes that flag only non-clean tasks: `sources-parse` and `unpinned-clone`.
- Codes that flag only clean tasks: **none**.
- Codes that flag every task in every class: `unpinned-image` and `verifier-isolation`.
- Codes that never flag a non-clean task: **none**; `verifier-network-download` and
  `verifier-runtime-install` flag clean tasks most often (44 and 46 of 49).

## Findings by executed class

| Class | Tasks | Findings | Mean findings per task |
|---|---:|---:|---:|
| `clean` | 49 | 194 | 3.96 |
| `reference_below_one` | 9 | 23 | 2.56 |
| `reference_unmeasured` | 10 | 39 | 3.90 |
| `no_reference` | 26 | 56 | 2.15 |
| `cannot_run` | 1 | 2 | 2.00 |
| **Total** | **95** | **314** | **3.31** |

Every task has at least 2 findings; this corpus has no 0-finding task.
The minimum is 2 and the maximum is 10.
The offline test module covers the 0-finding row with a synthetic package.

## Complete table

This table lists all 95 CSV rows in CSV order.

| Task | Executed class | Findings | Lint codes |
|---|---|---:|---|
| `acl-permissions-inheritance` | `reference_below_one` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `amuse-install` | `reference_unmeasured` | 10 | unpinned-clone, unpinned-image, unpinned-python-dependency, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `anomaly-detection-ranking` | `clean` | 2 | unpinned-image, verifier-isolation |
| `api-endpoint-permission-canonicalizer` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `application-debug` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `auth_token_race_condition` | `clean` | 3 | unpinned-image, verifier-isolation, verifier-network-download |
| `bandit-delayed-feedback` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `bash-log-processor-fix` | `reference_below_one` | 3 | sources-parse, unpinned-image, verifier-isolation |
| `basic-message-queue` | `no_reference` | 3 | unpinned-image, unpinned-python-dependency, verifier-isolation |
| `battery-charging-optimization` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `bloom-filter-cache-penetration-prevention` | `reference_below_one` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `book-portfolio-analysis` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `bracket-sequence-restoration` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `breast-cancer-mlflow` | `reference_unmeasured` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `broken-python` | `clean` | 3 | unpinned-image, verifier-isolation, verifier-runtime-install |
| `build-merkle-tree-cli-sha512` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `build-system-task-ordering` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `california-housing-api` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `chained-forensic-extraction_20260101_011957` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `competitive-programming-solver` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `container-registry-optimization` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `convolutional-layers` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `corrupted-filesystem-recovery` | `reference_below_one` | 2 | unpinned-image, verifier-isolation |
| `cosign-keyless-signing` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `cpp-daemon-sighup-segfault` | `reference_unmeasured` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `csv-json-jsonl-merger` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `db-migration-local-storage` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `distributed-test-execution-scheduler` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `ekf-localization` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `etl_checkpoint_resume_bug` | `cannot_run` | 2 | unpinned-image, verifier-isolation |
| `fix-js-network-controller` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `fix_async_worker_queue` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `floor-plan-geometry` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `game-of-stones` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `grid-pathfinding` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `grpc-plant-position-server` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `html-index-analysis` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `hydra-debug-slurm-mode` | `clean` | 6 | unpinned-image, unpinned-python-dependency, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `image-tile-identification` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `industrial-kiln-controller` | `clean` | 5 | unpinned-image, unpinned-python-dependency, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `iot-device-registration-server` | `reference_unmeasured` | 2 | unpinned-image, verifier-isolation |
| `iris-dataset-classification` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `jq-data-processing` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `jsonl-aggregator` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `legal-summary-extraction` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `log-summary` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `malicious-package-forensics` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `maven-slf4j-conflict` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `mech-system` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `mlflow-register` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `monorepo-changelog-cli` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `mtls-cert-rotation` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `multi-labeller` | `clean` | 5 | unpinned-image, unpinned-python-dependency, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `multi-server-configuration` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `network-log-normalization` | `reference_below_one` | 2 | unpinned-image, verifier-isolation |
| `neural-architecture-search-final` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `neutron-submission` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `okhttp-trailers-crash` | `reference_unmeasured` | 3 | unpinned-image, unpinned-python-dependency, verifier-isolation |
| `pandas-etl` | `reference_unmeasured` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `pandas-numpy-data-analysis` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `parking-lot-pathfinding` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `pdf-table-parsing` | `reference_unmeasured` | 2 | unpinned-image, verifier-isolation |
| `permutation-construction-100k` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `playing-card-recognition` | `no_reference` | 3 | unpinned-image, unpinned-python-dependency, verifier-isolation |
| `prediction-model-evaluation` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `protein-sequence` | `reference_below_one` | 2 | unpinned-image, verifier-isolation |
| `publisher-market-analysis` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `publisher-market-analysis-v2` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `python-api-rate-limit` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `raft-log-repair-concurrent-access` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `react-typescript-debugg` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `reproducibility-and-envsetup` | `clean` | 3 | unpinned-image, verifier-isolation, verifier-runtime-install |
| `reverse-engineer-stack-vm` | `reference_unmeasured` | 5 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `rsa-jwt-token-api-redis-blacklist` | `clean` | 5 | unpinned-image, unpinned-python-dependency, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `sakila-sqlite-queries` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `sales-data-csv-analysis` | `clean` | 2 | unpinned-image, verifier-isolation |
| `scan-linux-persistence-artifacts` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `schedule-vacation` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `security-breach-incident-response` | `reference_below_one` | 2 | unpinned-image, verifier-isolation |
| `security-incident-log-analysis` | `reference_below_one` | 2 | unpinned-image, verifier-isolation |
| `server-log-analysis` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `service-deployment-wave-planner` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `sign-vector-game` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `simple-database-query-tool` | `no_reference` | 2 | unpinned-image, verifier-isolation |
| `smiles-data-lab` | `reference_unmeasured` | 2 | unpinned-image, verifier-isolation |
| `sql-injection-forensics` | `reference_unmeasured` | 3 | unpinned-image, unpinned-python-dependency, verifier-isolation |
| `submission_a63937a5_20251224_152124` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-runtime-install |
| `supply-chain-fulfillment` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `symlink-chain-traversal` | `reference_below_one` | 2 | unpinned-image, verifier-isolation |
| `sympy-bug-fix` | `no_reference` | 4 | unpinned-clone, unpinned-image, unpinned-python-dependency, verifier-isolation |
| `task-xxe-exploit` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `todos-api` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `token-auth-websocket` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `vimscript-vim-quine` | `clean` | 4 | unpinned-image, verifier-isolation, verifier-network-download, verifier-runtime-install |
| `word-derangement-mapping` | `no_reference` | 2 | unpinned-image, verifier-isolation |

## Limits

- Lint findings are descriptive, not keep/fix/discard predictions
  (`src/evallab/dataset_audit_checks.py`). This README reports counts only.
- A class comes from one executed reference and one nop cell. It measures solvability and
  nop payoff, not contamination, exposed keys, or a grader that accepts a wrong answer.
- The join measures this card snapshot and this corpus commit. A later card or commit can
  change rows; rerun the builder against both.
- `unpinned-image` and `verifier-isolation` fire on all 95 tasks. Their counts say nothing
  about task health, and their presence must not be read as a failure signal.
- The scan reads source bytes only. It does not build an image, resolve a digest, or run a
  shell file.
