# HAR-117 results-home provenance

What MLflow and Weights & Biases record per run, and what the results home
records for the same facts. Research did not change the design: the brief's
list is the right shape, and both trackers already treat commit-plus-dirty-diff
as the way to reproduce a run made from uncommitted code. Findings are
recorded here; the implementation follows the brief.

## MLflow

System tags set automatically when a run starts inside a git checkout, from
`mlflow.tracking.context.git_context` / `resolve_tags` (tag constants in
[`mlflow/utils/mlflow_tags.py`](https://github.com/mlflow/mlflow/blob/master/mlflow/utils/mlflow_tags.py),
documented under
[System Tags](https://mlflow.org/docs/latest/ml/tracking/tracking-api/) and
[Track versions of Git-based applications](https://mlflow.org/docs/latest/genai/version-tracking/track-application-versions-with-mlflow)):

| MLflow tag | Meaning | Results home |
|---|---|---|
| `mlflow.source.git.commit` | HEAD sha at run start | `repository.commit` |
| `mlflow.source.git.branch` | branch name | `repository.branch` |
| `mlflow.source.git.repoURL` | `origin` remote URL | `repository.remote` |
| `mlflow.source.git.dirty` | uncommitted changes exist | `repository.dirty` |
| `mlflow.source.git.diff` | the uncommitted diff, stored on the run (newer MLflow; the gap was tracked as [mlflow#7017](https://github.com/mlflow/mlflow/issues/7017)) | `uncommitted.diff` next to the job, sha256 in `repository.uncommitted_diff_sha256` |
| `mlflow.source.name` | entry-point file | `command` (the full argv) |
| `mlflow.source.type` | `LOCAL`, `NOTEBOOK`, `PROJECT`, `JOB` | `source_type` (always `local` here; runs are local jobs) |
| `mlflow.user` | OS user | not recorded; not needed to reproduce a job |

MLflow captures these at run start, not later. A publish step that re-reads
git would attribute the publisher's checkout, not the code that ran.

## Weights & Biases

`wandb.init()` records git state automatically when the working directory is
inside a git checkout
([how to save the git commit](https://docs.wandb.ai/support/models/articles/how-can-i-save-the-git-commit-associated)).
The run's `wandb-metadata.json` holds host and program fields; code saving
adds the diff
([Save and diff code](https://docs.wandb.ai/models/app/features/panels/code)).
`WANDB_DISABLE_GIT` turns the probe off.

| W&B field | Meaning | Results home |
|---|---|---|
| `git.commit` | HEAD sha | `repository.commit` |
| `git.remote` | remote URL | `repository.remote` |
| `diff.patch` / `diff_<sha>.patch` | uncommitted changes against HEAD, written when code saving is on | `uncommitted.diff` |
| code artifact (`run.log_code`, or `code_dir`) | the program files | not copied wholesale; the harness tree, task package and variant digests pin the evaluated code instead |
| `program` | the script path | `command` |
| `args` | the script's arguments | `command` |
| `host`, `os`, `python`, `startedAt` | machine and clock | `host` (platform, python) plus the job's own `started_at` |

W&B also keeps the command line used to launch the run on the run page, hidden
from external viewers. The results home keeps the same line, with secrets
already stripped by the runner's metadata writer.

## What the lab already records

`lab-metadata.json` (written by `evallab.runner._write_run_metadata` at run
end) already has:

- `repository.commit` and `repository.dirty` (`runner.git_state`)
- `command` (the harbor argv, including `--model` and `--jobs-dir`)
- `host`, `tools.harbor`
- `experiment.harness_tree_path` / `harness_tree_sha256`,
  `task_package_digest`, `verifier_digest`, `task_id`
- `harness_tree` (rendered config, including the model) and `task_staging`
  (source and staged package digests)
- `model_identity.requested`

Missing at run time, and added by this change: remote URL, branch, worktree
path, the uncommitted diff and its sha256, and the names of untracked files.
Commit and diff have to be taken when the job runs. A later publish reads a
checkout that has moved on, so it cannot reconstruct them. The runner now
saves that snapshot into `lab-metadata.json` and `repository-provenance/`
inside the job directory. Publish copies it verbatim. When process-job writes
its reports to a custom `output_dir`, publish still snapshots the raw job
directory but takes `processed/` exclusively from the newly written
`job`/`trial` report pages, excluding obsolete source pages and unrelated
out-dir files. The published tree and INDEX therefore show the new outcome.
The explicit report directory must exist outside the results home; invalid
inputs fail before the existing publication is replaced.

## Publish-time only

- **PR number.** A PR may not exist when the job starts. Publish asks `gh`
  which PR contains the recorded commit. Tests pass a lookup function and
  never hit the network. When `gh` is missing or fails, the value is `null`
  with a reason, never `0`.
- **The Linear card.** Taken from the job name or the spec's `question_ref`
  (`har110-000495-plain` and `har110-python-gepa` both give HAR-110). Unknown
  when neither matches.
- **The research doc link.** A search of the primary checkout for a
  `research/**` doc whose path contains the card slug. Absent when none
  matches.

## INDEX reward columns

Each INDEX row keeps two verdicts side by side. `Reward (raw)` is the
verifier pass/fail/unscored from the job report and never changes. `Counted`
is the `evallab.counts` verdict stored in the same report summary
(`n_counted_pass` / `n_counted_fail` / `n_excluded` / `excluded_reasons`), so
the INDEX matches the canonical report. A raw pass excluded as `copied_fix`
reads as a raw pass and a counted exclusion on the same row. Reports that
predate counts have none of those fields and render `counts unknown`, never
`0`.

## Token facts and attribution

Processed trial pages use **proxy-settled** input/output tokens from
`lab-metadata.json#provider_usage`, validated through `ledger.split_usage`.
They never substitute Harbor's native counters or divide a job's usage
equally among trials. A single-trial job permits attribution of the job
ledger; a multi-trial job has unknown per-trial proxy usage unless a
per-trial meter exists. The complete job ledger remains in `job.json`.
Missing or invalid ledgers stay unknown, not zero.

The report keeps three distinct measurements:

- `tokens_proxy`: settled usage with `source`, `scope`, `attribution`, and
  an unavailable-data reason where needed. Decision token facts use this.
- `tokens_native`: Harbor's `result.json#agent_result` counters.
- `tokens_steps`: the stitched-step sum. Existing `tokens_used` summary
  fields retain this step-sum meaning; they are **not** settled proxy totals.

`tokens_attempted_proxy` is the settled usage plus unresolved reservations,
with the same single-trial attribution requirement, not an equal split.
It is a ceiling footprint, not an additional settled usage measurement.

HAR-116 `har116-a-001181-baseline` demonstrates the distinction: native
input/output is **2,410,295 / 7,444**, while its 89 settled ledger calls
sum to **2,423,707 / 11,540**. The permanent regression fixture retains the
actual accounting fields plus hashes of the original metadata and result.
Raw source artifacts are not rewritten during reprocessing.

## Shared-GPU estimates

A self-hosted `cost_estimate_usd` is a per-trial wall-time estimate, not a
metered share of the common server. Overlapping jobs must not be summed as
though each owned the GPU. Until a billed-session allocation is available,
both pages and INDEX label these values **shared GPU, not additive** and
direct the reader to `evallab spend day`. The existing Modal billing and
Daytona estimate authorities remain separate; no zero-priced proxy ledger
is presented as free GPU serving.

## Billed-session allocation

Once the Modal bill for a shared-GPU session lands, reprocessing the job
with `evallab process-job --session-spend <receipt>` replaces the
non-additive estimate in the job report and INDEX row with its allocated
share: billed GPU pool split by recorded trial wall time over the complete
session membership, plus the existing per-job Daytona estimate
(`report['summary']['session_spend']`, basis
`billed_modal_wall_time_share_plus_daytona_estimate` with the session id).
Settled proxy cost and tokens are untouched and no per-trial GPU share is
invented. An unknown Daytona estimate renders the GPU share plus unknown
sandbox, never a full total and never the legacy wall-time estimate.

Bill-before-allocation boundary: metering happens before the bill and
allocation only after it, so ordinary landing pages stay non-additive
until the billed receipt arrives, and reprocessing requires passing the
receipt again. The receipt is validated before any report is written or
any publication replaced: a stale or wrong receipt fails the run and the
previous publication stands. Raw job inputs are never rewritten.

The `evallab.session_spend/v1` receipt binds native job/spec identities and
the exact metadata bytes to complete teardown membership. It retains Modal
billing rows and deployment provenance separately from the Daytona estimate.
Overlapping hourly/daily billing intervals are rejected rather than added.
The allocation is an accounting policy, **not measured per-job GPU use**.

The [HAR-116 receipt](har131-session-spend.json) covers two 20-job sessions:

| Session | Billed Modal | Daytona estimate | Combined target | Displayed sum |
|---|---:|---:|---:|---:|
| `ap-i1jDXiUmYYzX3k11Tpww83` | $1.53380886 | $0.49377610 | $2.02758496 | $2.0277 |
| `ap-yXxcAQPhR4WRVkUd4thsUc` | $1.67625561 | $1.10407693 | $2.78033254 | $2.7805 |
| Total | $3.21006447 | $1.59785302 | $4.80791749 | $4.8082 |

The [runtime proof](har131-spend-proof.json) records an isolated results-home
publication over all 40 actual jobs, using their raw metadata rather than new
trials. Both session sums differ from the source total by less than 0.01%
(display rounding), within the requested ±10%. This receipt excludes other
cards' sandbox spend, later G4 apps, and overlapping hourly copies of bills.

## Backfill

Jobs that predate the run-time snapshot have `repository.commit` and
`repository.dirty` only. Their diff is gone. Backfill records
`capture: "publish-time"` when the worktree still exists and git answers, and
`capture: "unknown"` with the reason when it does not. It never invents a
diff, a PR number, or a spend figure.
