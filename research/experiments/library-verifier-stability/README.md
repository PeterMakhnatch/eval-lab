# Library verifier stability (HAR-457)

Cost: **$0**. Run: `har457-20261116-e75b174a`. Launch commit: `707eea4db2ae1cc23ad3e587f1d80478d4c2af23`.
Host architecture: `arm64`. Docker architecture: `aarch64`.

## Measured results

| package | classification | rewards (0,1,2) | published job |
| --- | --- | --- | --- |
| query-optimize | stable | 1.0, 1.0, 1.0 | 2026-11-16/HAR-457-har457-20261116-e75b174a-query-optimize-oracle-r3 |
| html-js-filter | stable | 1.0, 1.0, 1.0 | 2026-11-16/HAR-457-har457-20261116-e75b174a-html-js-filter-oracle-r3 |
| event-summary | stable | 1.0, 1.0, 1.0 | 2026-11-16/HAR-457-har457-20261116-e75b174a-event-summary-oracle-r3 |
| release-branch-rescue | stable | 1.0, 1.0, 1.0 | 2026-11-16/HAR-457-har457-20261116-e75b174a-retry1-release-branch-rescue-oracle-r3 |
| transaction-reconciliation | stable | 1.0, 1.0, 1.0 | 2026-11-16/HAR-457-har457-20261116-e75b174a-transaction-reconciliation-oracle-r3 |
| syn-funcdag-medium | stable | 1.0, 1.0, 1.0 | 2026-11-16/HAR-457-har457-20261116-e75b174a-syn-funcdag-medium-oracle-r3 |

Published paths are relative to `~/Developer/eval-lab-results`.
`jobs.json` selects exactly these 6 final jobs and explicitly retains any prior attempts.
The builder never scans other jobs.
[stability.csv](stability.csv) preserves every repeat reward, error, exit code, and stdout digest.

### Retained prior attempts

| package | published job | native error | n_runs |
| --- | --- | --- | --- |
| release-branch-rescue | 2026-11-16/HAR-457-har457-20261116-e75b174a-release-branch-rescue-oracle-r3 | EnvironmentStartTimeoutError: Environment start timed out after 600.0 seconds | unknown (no stability.json) |

The initial release job records `EnvironmentStartTimeoutError` before the verifier starts.
The error reports `Environment start timed out after 600.0 seconds`.
That message names the configured limit, not a measured 600-second wait.
The trace includes a daemon-information query; it does not identify the root cause.
The root cause is unknown. This finding does not establish a Docker daemon outage.
The startup failure, not a reward, triggers the permitted fresh dispatch.

An absent stability record means no repeat reward is measured; it does not mean reward 0 or a flip.
The table retains the initial job even when a fresh dispatch completes.
Any successful fresh dispatch is a separate job, not another attempt inside the initial job.
Each dispatch still requests `attempts=1`. No retry is selected by reward.

### Evidence findings

- **query-optimize**: none
- **html-js-filter**: none
- **event-summary**: none
- **release-branch-rescue**: none
- **transaction-reconciliation**: none
- **syn-funcdag-medium**: none

### Query runtime margin

| repeat_index | golden_median_s | solution_median_s | ratio | limit | comparison | error |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 0.951905125984922 | 0.9491043749731034 | 0.9970577414330858 | 1.05 | within | none |
| 1 | 0.9484294180292636 | 0.9482454999815673 | 0.9998060814603594 | 1.05 | within | none |
| 2 | 1.0118135010125116 | 1.0156408760230988 | 1.0037826882194765 | 1.05 | within | none |

[runtime.csv](runtime.csv) records all 3 repeats, including absent measurements.
The builder uses the existing [query parser](../query-optimize-runtime-margin/build.py).
It selects the first printed golden and solution `median_s` values.
The ratio is `solution / golden`; `within` means the ratio is at most `1.05`.
A missing, nonfinite, or zero-denominator measurement remains `unknown`, never zero.
The threshold comparison does not prove query correctness or a stable timing margin on other hosts.

### HTML browser caveat

| repeat_index | batches_run | timed_out_batches | error |
| --- | --- | --- | --- |
| 0 | 28 | 1 | none |
| 1 | 28 | 1 | none |
| 2 | 28 | 1 | none |

These counts use the existing [HAR-424 parser](../html-js-filter-unexecuted-vectors/build.py) on each repeat stdout.
It counts exact `RUN BATCH` lines and at most 1 `Failed to load URL:` event per batch.
The [HAR-424 table](../html-js-filter-unexecuted-vectors/trials.csv) separately records 3 oracle browser trials.
Each records 28 batches, 1 timed-out batch, and reward 1.
Those older trials are context, not repeat evidence for this campaign.
The [source explanation](../html-js-filter-unexecuted-vectors/README.md#why-the-verifier-accepts-the-timeout) shows why the verifier can accept a timeout.
A timed-out batch is not independent proof that the filter blocks its vectors.
The printed `contains 0 tests` value is not a vector count.
Full batches contain 16 vectors; the final batch can contain 12.
Zero browser batches does not prove successful browser execution.

## Method and classification

Each job requests `Executor.execute_direct(RunRequest(agent="oracle", environment="docker",
verifier_repeat_n=3, concurrency=1, attempts=1, ...), ingest=False)`.
The campaign runs 1 job at a time through the default host-wide Docker FIFO.
The active Harbor watchdog is 5,400 seconds per job. FIFO waiting is separate.
No model, paid service, task change, or harness change forms part of this experiment.

The [repeat verifier](../../../src/evallab/harbor_repeat_verifier.py) invokes the native verifier 3 times.
All invocations use one final oracle state and the same verifier environment.
They are not independent oracle attempts, fresh environments, or clean-state rollbacks.
Earlier verifier side effects can affect later invocations.
Separate mode transfers declared artifacts to one verifier environment.
One native verifier deadline covers the complete repeat call; it does not multiply by 3.
Separate-mode errors remain findings, not permission to change the task or harness.

- **stable**: all 3 finite rewards agree, and the required evidence has no errors.
- **flipped**: all 3 finite rewards exist and differ, with no evidence errors.
- **error**: any repeat error, absent reward, missing evidence, native exception, or nonzero recorded exit code.
- A stable reward 0 is not an oracle pass. Only three rewards of 1 support repeat oracle success.
- Three failed or missing repeats never count as stable.

Published native files are authoritative. Manifest identities must match native identities when both exist.
`source_package_digest` identifies the complete source package in native staging metadata.
The builder checks it against the manifest and native `experiment.package_digest`.
It also checks the source path in native experiment provenance.

The hashes have different algorithms and file selections:

- **Source package:** `registry.task_directory_digest` sorts recursive package files under its ignore policy.
  It hashes concatenated `file_sha256_hex + two spaces + ./relative_path + LF` records.
- **Registry staging:** `registry.harbor_task_digest` selects root `task.toml`, `instruction.md`, `README.md`,
  and recursive files below `environment`, `tests`, `solution`, and `steps`, under its fixed ignore policy.
  It sorts relative paths and hashes `relative_path + NUL + file_sha256_hex + LF` records.
- **Native lock:** Harbor `Packager.compute_content_hash` uses that flat record encoding.
  Its file selection also supports `trajectory.json` and task `.gitignore` rules.
  Therefore, equality with the registry helper is an observed binding, not a universal algorithm equivalence.
  `lock_task_digest` records the authoritative executed identity from trial `lock.json.task.digest`.
- **Legacy result checksum:** `task_digest` records native `result.json.task_checksum`.
  Harbor `Task.checksum` calls `dirhash(task_dir, "sha256")` over the whole staged directory.
  It applies no package ignore list and omits empty directories by default.
  File descriptors contain `data:file_sha256_hex` and `name:basename`.
  Directory descriptors contain `dirhash:child_hash` and `name:basename`.
  It sorts properties, joins them with NUL, sorts entry descriptors, and joins entries with 2 NUL bytes.
  Each directory hash is the SHA-256 of that UTF-8 descriptor. The checksum has no `sha256:` prefix.

The runner adds root `run_manifest.json` after validating the copied package.
This file enters the legacy whole-directory checksum, but not the package-lock or registry-helper allowlists.
The native lock and legacy checksum are recorded before environment setup and oracle execution.
The observed query lock digest matches its staging digest; its legacy checksum differs.
This difference does not establish task drift or oracle-time mutation.

Algorithm sources are [`registry.py`](../../../src/evallab/registry.py),
[`runner.py`](../../../src/evallab/runner.py), and the locked Harbor and `dirhash` runtime sources.
Harbor owners are `models/task/task.py`, `models/job/lock.py`, and `publisher/packager.py`.
The `dirhash` owner's `Protocol.get_descriptor` defines its recursive descriptor encoding.

The builder binds lock type to `local`, lock name to the staged task name,
lock path to native `result.config.task.path`, and lock digest to the staging digest.
Missing lock identities produce evidence errors; identity mismatches refuse the report.
It compares manifest `task_digest` only with the legacy result checksum, never with a flat package hash.
It also checks the source basename and native trial name.
It does not recalculate identity from mutable checkout task bytes.
Missing native identity stays `unknown` and produces an evidence error.

## Digest and exit-code semantics

Canonical per-repeat stdout is `verifier/repeat/{0,1,2}/test-stdout.txt`.
The verifier restores top-level files from repeat 0; top-level stdout is not repeat 1 or 2.
`stdout_tail_sha256` hashes only the final 8,192 stdout bytes, not the full file.
Missing or empty stdout has a null native digest, rendered as `unknown`.
A null digest with empty or unavailable stdout is allowed by the native v1 schema.
It does not by itself change equal-reward stability into an error.
`stdout_error_i` preserves availability notes as well as digest errors.
Missing query medians remain unknown; absent HTML browser batches remain an evidence caveat.
The builder recomputes each available tail digest and reports mismatches as evidence errors.
The digest does not establish full-output identity or task correctness.

`exit_code_i` records the last verifier shell/script exec, excluding preparation calls.
It does not necessarily equal an inner pytest exit code.
`command_exit_code` records `lab-metadata.json.exit_code`, the native Harbor subprocess status.
It does not record the dispatcher status or an individual verifier invocation.
`dispatcher_exit_code` separately records the manifest's dispatcher status when available.
Harbor can parse a reward after a nonzero verifier exit. A command exit of 0 does not prove a pass.
Run 0 determines the ordinary native trial reward; this table uses all 3 repeat records.
`none` means an observed absent error; `unknown` means an absent measurement.

## Reproduce without executing tasks

From the owned repository worktree:

```bash
.venv/bin/python research/experiments/library-verifier-stability/build.py
.venv/bin/python research/experiments/library-verifier-stability/build.py --check
# Before the parent copies the manifest:
.venv/bin/python research/experiments/library-verifier-stability/build.py --manifest /path/to/jobs.json
# Optional alternate published root and output directory:
.venv/bin/python research/experiments/library-verifier-stability/build.py \
  --manifest /path/to/jobs.json --root /path/to/eval-lab-results --out-dir /tmp/har457-render
```

`--manifest` defaults to this folder's `jobs.json`.
`--root` defaults to `EVALLAB_RESULTS_ROOT`, then `~/Developer/eval-lab-results`.
`--out-dir` defaults to this folder. Outputs are `stability.csv`, `runtime.csv`, and `README.md`.
`--check` changes no files. Exit 0 means exact reproduction; exit 1 means output drift.
Exit 2 means an invalid manifest, unsupported schema/count, identity mismatch, or unusable results root.
Measured trial failures still render rows with `error`; they do not become synthetic rewards.
The builder never modifies the manifest or published evidence.
