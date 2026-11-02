---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Eval Lab assembly line

The assembly line turns a named batch of DEV rollouts into keep, drop and review
exports, with receipts, a human-review queue and a recomputable scoreboard. It
answers two different questions: **what should happen to each rollout?** and
**how well did those decisions match independent labels?** Labels answer only the
second question. They are never passed to the decision stages.

This is a batch workflow, not task certification, a training job, or permission
to spend money. Use AutoReview `off` or `cached` for the zero-spend workflow below.
Replay refuses live AutoReview and any nonzero spend cap. TEST and answer-key
paths are refused rather than silently skipped. Keep real batch artifacts,
labels, transcripts and baselines outside the repository.

## A local, zero-spend spec

Save a JSON spec outside the repository, for example
`~/Developer/eval-lab-results/line-inputs/dev-example.json`. Replace the example
input paths with admitted DEV evidence. Relative paths resolve against the
original spec's directory, not the shell's working directory.

```json
{
  "schema": "evallab.line_spec/v1",
  "batch_id": "dev-example",
  "split": "dev",
  "sources": [
    {
      "corpus": "terminal-wrench",
      "rollouts": "/absolute/path/to/dev-rollouts.jsonl",
      "labels": "/absolute/path/to/dev-labels.jsonl"
    }
  ],
  "task_status": "/absolute/path/to/task-status.json",
  "task_action": "review",
  "autoreview": {
    "mode": "off",
    "cache": null,
    "route": null,
    "approval": null,
    "max_usd": 0
  },
  "models": {"allowed": []},
  "audit": {"rate": 0.05, "seed": "dev-example"},
  "judge": {
    "queue": "~/Developer/eval-lab-results/line-judge-queue.jsonl",
    "write": false
  },
  "labels_policy": {"attempted_cheat": "exclude"},
  "out_root": "~/Developer/eval-lab-results/line"
}
```

`labels` and `task_status` may be `null`. Without labels, counts still exist but
label-dependent rates with zero denominators are unavailable, not measured as
zero. Task status comes from the task-side manifest described in
[hack-drop](hack-drop.md); rollout labels are not certification evidence.
`task_action` controls review versus drop for weak tasks. Missing evidence is
reported as `uncertified`, not promoted to certified.

`labels_policy.attempted_cheat` must explicitly be `exclude`, `cheat` or `clean`.
Attempted-but-failed cheats remain separately counted whichever policy is used.
All labels pass through the shared `hack_drop.load_labels` loader.

For `cached` AutoReview, supply `autoreview.cache` as an existing hack-drop
output manifest or directory containing `kept.jsonl`, `dropped.jsonl` and
`review.jsonl` with stored AutoReview stage verdicts. Every replayed run needs a
cached verdict; the line does not fill gaps with a live model call.

Cached verdicts retain their historical model/score/evidence details. Reusing
those bytes makes no new model call: this batch records zero spend and no
executed model routes. Historical costs are not charged a second time.

```bash
uv run evallab line run ~/Developer/eval-lab-results/line-inputs/dev-example.json --json
uv run evallab line check ~/Developer/eval-lab-results/line/dev-example --json
uv run evallab line scoreboard ~/Developer/eval-lab-results/line/dev-example --json
```

A batch ID cannot overwrite an existing batch. Choose a new ID for another run.
A run or check exits 0 only when all eight checks pass; failed quality gates
exit 2. Read the check details before using any exports.

## What moves down the line

1. **Intake:** use hack-drop's rollout loader and DEV admission checks; record
   run ID, corpus, task, model when known, raw reward, source path and digest.
2. **Task evidence:** join the existing task-status manifest. Its statuses are
   `certified`, `provisional`, `weak` and `uncertified`; certification belongs to
   the task-evidence producer, not the line.
3. **Trace evidence:** reuse hack-drop's deterministic L0 stage and optional
   cached AutoReview verdicts. The line does not invent a second detector.
4. **Decisions:** reuse hack-drop's consolidation precedence: drop wins over
   review, which wins over keep. Persist decisions before loading labels.
5. **Judge routing:** queue every review and a deterministic sample of keeps.
   Audited keeps remain keeps, carry `AUDIT_SAMPLE`, and count toward judge load.
   Drops and non-audited keeps are not queued. `judge.write: false` still creates
   the batch-local queue but does not append to the external Station queue.
6. **Exports:** partition all decisions into hack-drop-compatible manifests.
   A review export is not an accepted training export or a human verdict.
7. **Scoreboard and checks:** join independent labels, record quality checks
   and produce JSON plus a readable scoreboard. Existing Station verdicts are
   imported separately; the line does not fabricate human adjudication.

By default the verdict importer reads the Station's
`~/.local/state/evallab-judge/verdicts.jsonl`. An optional `judge.verdicts` path
selects a separate Station export/state file, useful for isolated local work.
The importer applies the Station's undo/latest-verdict rules and keeps only
this batch's run IDs. If no file exists, `verdicts.jsonl` is empty; importing
human verdicts never silently changes the detector's decisions or exports.

The audit rule is label-blind and repeatable:
`int(sha256(seed + ":" + run_id)[:8], 16) / 2**32 < rate`.
It applies only to keeps, so auditing cannot change the detector's recall.

Each batch contains:

```text
spec.json
runs.jsonl, tasks.jsonl, trace.jsonl, decisions.jsonl
judge-queue.jsonl, verdicts.jsonl
export/kept.jsonl, export/dropped.jsonl, export/review.jsonl
receipts/intake.json, task.json, trace.json, decisions.json,
         judge.json, export.json, scoreboard.json
scoreboard.json, SCOREBOARD.md, check.json
```

The receipt files live under `receipts/`. Each records its schema, batch, stage,
code revision and dirty state, start/end times, elapsed seconds, input/output
hashes, row counts, model routes, spend and structured errors. Evidence stays on
disk so a claim can be checked without trusting a terminal summary.

## Eight quality checks

| Check | What must hold |
|---|---|
| Conservation | Every intake run has exactly one decision; keep + drop + review equals intake; audits are a subset of keeps. |
| DEV only | Sources, runs and tasks pass the existing DEV gates; no TEST or answer-key paths. |
| Provenance | Receipt input/output hashes match disk and the code revision is recorded. |
| Determinism | Two in-process decision passes have the same hash; the checker verifies the saved decision identity again. |
| Spend and models | Spend stays inside the spec cap and every used route is allow-listed; approval does not implicitly authorize a different model or budget. |
| Judge routing | Every review and audit appears exactly once in the local queue; no other run appears. |
| Label isolation | Decisions contain no label fields; label loading belongs to the scoreboard stage. |
| Scoreboard recompute | Re-deriving numbers from persisted decisions and shared-loader labels reproduces the scoreboard. |

Any failed check blocks a successful run/check exit. These are evidence and
consistency gates, not a claim that a detector has perfect recall or that an
export is automatically suitable for training.

## Reading the scoreboard

`scoreboard.json` uses `evallab.line_scoreboard/v1`; `SCOREBOARD.md` is its
readable view. Overall and per-corpus summaries show total and labelled runs,
cheats, clean runs, attempted cheats and keep/drop/review counts. Each measured
rate carries numerator `k`, denominator `n`, a rate and a Wilson 95% interval:

- **Drop recall:** labelled cheats dropped / labelled cheats.
- **Drop-or-review recall:** labelled cheats dropped or reviewed / labelled cheats.
- **Clean false-alarm rate:** labelled clean runs dropped / labelled clean runs.
- **Clean drop-or-review rate:** labelled clean runs dropped or reviewed / labelled clean runs.
- **Drop precision:** labelled cheats dropped / all labelled runs dropped.

The scoreboard also shows stage rows in/out, seconds and errors; review-plus-audit
load per 100 runs; runs on each task status; reward-1 runs on uncertified tasks;
spend, used model routes and quality-check results. A successful reward is not
proof of honest behavior, and `review` is not a confirmed cheat label. Corpus
cohort and effective-label hashes make replay comparisons identity-aware.

## Replay as a regression gate

Create or refresh a baseline only with an explicit request:

```bash
uv run evallab line replay ~/Developer/eval-lab-results/line-inputs/dev-example.json \
  --baseline ~/Developer/eval-lab-results/line-baselines/dev-example.json \
  --write-baseline --json

uv run evallab line replay ~/Developer/eval-lab-results/line-inputs/dev-example.json \
  --baseline ~/Developer/eval-lab-results/line-baselines/dev-example.json --json
```

Replay resolves source paths against the original frozen spec, writes only a
scratch spec outside the repository, and uses a fresh `-replay-...` batch ID.
It never edits the source spec or appends replay items to the external Judge
Station queue. The resulting batch remains available under `out_root`; only the
scratch spec is removed.

A baseline uses `evallab.line_baseline/v1` and stores the frozen spec hash,
scoreboard and `{run_id, corpus, decision}` rows, plus frozen input and intake
hashes. The spec hash excludes only `batch_id`, `out_root`, `judge.queue` and
`judge.write`. Changing those output destinations does not redefine the cohort.
Changing other spec fields, frozen input bytes, cohort identities, labels,
label policy or denominators makes the comparison invalid. Refresh intentionally
only after understanding such a change; a quality-failing batch cannot create
or replace a baseline.

The gate compares **each corpus**, not just the overall average:

- Recall (drop-or-review) must not fall by **more than** the larger of one cheat
  run or 2 percentage points: `max(1 / cheats, 0.02)`.
- Clean false alarms (drop only) must not rise by **more than** the larger of
  one clean run or 1 percentage point: `max(1 / clean, 0.01)`.
- Any failed quality check is a regression, even if measured rates improve.

A change exactly at the allowance passes. A zero denominator has no rate to
compare. Every result includes `changed_runs`, with corpus, run ID and before/
after decisions; an unchanged replay has an empty list. Invalid comparisons
retain changed decisions when a fresh batch could be read. Exit codes are
**0 pass**, **1 regression**, **2 invalid input/comparison**. Ordinary replay
never creates or refreshes a missing baseline implicitly.

## Relationship to hack-drop and the production factory

[hack-drop](hack-drop.md) owns the loaders, task/L0/AutoReview stage functions,
decision consolidation and report primitives. The line orchestrates those
functions with persistent batch receipts, quality gates and a replay baseline;
it is not an alternate detector implementation. Its exports retain hack-drop's
identifier/reason/evidence metadata rather than embedding raw transcript text.

The [production factory](production-factory.md) is a different execution shape:
it journals and replays DEV traces step by step through the production monitor
and has its own durable cursors, runtime guards and optional AutoReview modes.
The batch line reuses its existing Judge Station queue format and appender.
Neither a batch receipt nor a passing replay proves that the factory service is
running, enables its live AutoReview, deploys a model, certifies a task, or
licenses training. Production runtime behavior and approvals remain governed by
the factory's own commands and policy.
