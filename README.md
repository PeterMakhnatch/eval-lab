# Eval Lab

Eval Lab runs agents through [Harbor](https://github.com/harbor-framework/harbor),
keeps their evidence, and measures reward-hacking detection and task validity.
A verifier reward is an observation, not proof of an honest solve.

This README is the native operator entry. The lab keeps 5 functions and 1
measurement. Labelled reward-hacking traces, with the cheating action identified,
are the centre of the lab.

## Choose the operation

| Function | Native entry | Contract |
|---|---|---|
| **Run** | `evallab run`, `evallab process-job`, `evallab view` | Execute a named task. Keep the raw job, provenance, rewards, errors, and reports. |
| **Gold** | `evallab.gold.load_rows`, `evallab.gold.load_trajectory` | Read the existing IssueBank registry and hash-bound action coordinates through one loader. |
| **Detect** | `evallab line`, `evallab review` | Judge a finished run with L0 rules and readers, including AutoReview. Keep keep/review/drop decisions distinct. |
| **Watch** | `evallab watch` | Observe a running job through live watch, automatic attachment, and the causal monitor. Keep dispatch protection. |
| **Certify** | `evallab certify-gold`, `evallab harden` | Require strict G1–G7 evidence and @10 hardening. A package-check packet is not a Gold certificate. |
| **Score** | `evallab score` | Measure frozen cached DEV outputs. Keep detection, live monitoring, task QA, and grader QA contracts separate. |

Use `uv run evallab <command> --help` for installed options. The CLI still contains
older products during consolidation. Their presence is not a recommendation or
execution approval. This guide does not claim that every contract migration is
complete.

## Prepare the environment

Use Python 3.12+, [uv](https://docs.astral.sh/uv/), and the locked native dependency
profile:

```bash
uv sync --locked --no-group observability --group benchmarks --group lance --group inspect --extra laminar
uv run python -c "import evallab.cli"
uv run evallab --help
```

Docker with Compose v2 is required for local task execution, not for CLI help or
cached scoring. Harbor 0.24.0 comes from the `laminar` extra. An older global
Harbor installation does not replace the project environment.

## Run and retain evidence

Before a trial, read [execution tiers](docs/execution-tiers.md) and the selected
backend and agent policy. Free local controls require no model key. Run the
reference and no-op controls as separate jobs:

```bash
uv run evallab run --task library/tasks/event-summary --agent oracle --name event-summary-oracle
uv run evallab run --task library/tasks/event-summary --agent nop --name event-summary-nop
```

The reference must return reward 1. The no-op must return reward 0. These controls
check task validity, not model capability or full certification.

Raw Harbor jobs are immutable source evidence. Jobs start under ignored `runs/`.
`evallab process-job <job-dir>` processes a finished job and publishes it to
`~/Developer/eval-lab-results/`. Use `evallab view <job-dir>` for the native viewer.
`evallab results-viewer` serves the published results without changing raw jobs.
Processing can require the configured catalog and existing detector dependencies.
A catalog query error is not an empty catalog.

Task versions use content digests. Create a new variant instead of editing a
promoted version. Preserve lineage, native error outcomes, and missing rewards.
An unscored trial is not reward 0. See [task variants](docs/task-variants.md) and
[the evidence retention policy](docs/GENERATED-CACHE-POLICY.md).

## Gold data and offline detection

Use `evallab.gold.load_rows(repo_root, state_root=None)` for registry rows.
Use `evallab.gold.load_trajectory(row, repo_root=repo_root)` for a row's trajectory.
The loader checks split admission and source hashes. It resolves source action
coordinates without inventing new identifiers or labels.

Inspect aggregate availability without reading held-out traces:

```bash
uv run evallab failures bank gold-counts --json
```

Keep executed-positive, reviewed-control, attempted, and unjudged states separate.
A null or attempted label never becomes a binary control. The 156-row binary
action seed does not replace the 690-row Line policy pool. Preserve exposure
records, confinement, quarantines, frozen receipts, and commit-pinned provenance.

Use `evallab line --help` for finished-run processing and `evallab review --help`
for review. [The Line contract](docs/line.md) owns decisions, frozen prompts,
thresholds, labels, and gate limits. This guide changes none of them. Keep
finished-run Line scheduling separate from online watch. Do not report a raw
verifier pass as an honest pass without its review evidence.

## Watch a running job

```bash
uv run evallab watch --runs-dir /absolute/path/to/job --out /absolute/path/to/new-watch-state --once
```

Dispatch attaches the automatic watch to agent jobs. Oracle and no-op controls
skip that attachment. Explicit watch state contains derived observations, not
canonical rewards. Keep live watch, automatic watch, and the causal monitor as
the watcher core. Do not restore stop actuators or optional supervisor writers.

File-activity stall uses 600 seconds. Observed-step silence uses 2,700 seconds.
These signals use different inputs; do not merge their thresholds. Preserve
file-access capture, budgets, allowlists, and dispatch protection. This guide
does not install, unload, or change any service.

## Certify a task and its grader

Certification requires strict G1–G7, not an all-AST rule or 2-alternative acceptance:

- G1: the reference returns reward 1.
- G2: the no-op returns reward 0.
- G3: both frozen witness classes fail natively.
- G4: startup contamination returns reward 0.
- G5/G6: the frozen DEFECT-only train pool and sealed eval pool are nonempty.
  Every scheduled cell executes. Every DEFECT returns reward 0.
- G7: all 4 preregistered, uninformed alternatives return reward 1.

Keep exposure and freeze checks. Missing, fractional, unapplied, and infrastructure
outcomes never count as kills. POSSIBLY_VALID and base-eval results remain
diagnostic. Require @10 hardening without degraded or single-container bypasses.
Keep its 4 layers, runtime manifests, task security checks, and publication/hash
binding. Older certificates or implementation paths do not prove this contract.

Use `evallab certify-gold --help` and `evallab harden --help` to inspect the native
entries. Certification execution can use paid compute. Help is not approval to
execute it. Do not use the retired agent/round factory or Workbench as an operator
route.

## Score cached DEV evidence

Scoring starts no detector, task run, or model call. Supply an existing freeze and
a new output directory:

```bash
uv run evallab score --freeze /absolute/path/to/freeze.json --out /absolute/path/to/new-score-output
```

The freeze pins inputs, code hashes, cohorts, and thresholds. Changed hashes stop
scoring. Missing predictions stay missing. Report the cohort, denominator,
coverage, confounds, and recorded costs with every result. DEV evidence is not
held-out evidence. Unavailable live-watch or QA measurements stay unavailable.

[Cached scoring](docs/score.md) describes output files and metric limits. Keep
full receipts and per-item joins outside Git. Commit only the small aggregate
report and its receipt hashes.

## Approval and delivery boundaries

Model calls and rented compute require current approval under
[standing approvals](policy/standing-approvals.yaml) and
[execution tiers](docs/execution-tiers.md). Credentials and old caps do not grant
approval. Do not drain a queue, change a service, or deploy shared code as a smoke.
[Operations](docs/operations.md) owns recovery procedures.

Build changes in an isolated topic worktree. Keep the primary checkout on `main`.
Follow [AGENTS.md](AGENTS.md), [the workflow](agents/WORKFLOW.md), and
[the exact-head CI contract](agents/CHECKS.md). Use
`uv run evallab ship <PR>` from a stable checkout outside the topic worktree.
[The ship runbook](docs/ship.md) covers protected merge, receipts, and retirement.
Decision-sensitive changes require the registered, base-owned Line gate. Preserve
its limits and frozen inputs. A documentation change does not authorize a gate
exception.

## Historical manuals

The older cross-product manuals remain for historical evidence, not current
operator instructions. Each page links its source at
`7f2d2eed8c8dd80369d3fca2e0889f640ae964a6`. Read commands and relative references
against that revision. Do not use them to restore retired tools or approvals.

- [Harbor viewer](docs/harbor-view.md)
- [Run explorer](docs/run-explorer.md)
- [Live watch](docs/live-watch.md)
- [Task quality](docs/task-quality.md)
- [Trace harness](docs/trace-harness.md)
- [Observability](docs/observability.md)
- [MiMo task catalog](docs/mimo-task-catalog.md)

2026-11-10: Consolidate the native operator entry. Mark the older manual generation
historical without changing source code, evidence, policies, or service state.

## License

Apache-2.0; see [LICENSE](LICENSE). Third-party material keeps its own license.
Vendored packages under `library/external/` carry their upstream `LICENSE` or
`PROVENANCE.json`. Adapted files name their source in their headers.
