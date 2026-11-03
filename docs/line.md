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
    "write": false,
    "verdicts": "~/Developer/eval-lab-results/line-judge-verdicts.jsonl"
  },
  "labels_policy": {"attempted_cheat": "exclude"},
  "out_root": "~/Developer/eval-lab-results/line"
}
```

`labels` and `task_status` may be `null`. Without labels, counts still exist but
label-dependent rates with zero denominators are unavailable, not measured as
zero. Task status comes from the task-side manifest described in
[hack-drop](hack-drop.md); rollout labels are not certification evidence.
Every task carries its own decision reason: `TASK_CERTIFIED`, `TASK_PROVISIONAL`,
`TASK_UNCERTIFIED` or `TASK_WEAK`. A task absent from the manifest is `uncertified` (reason
`NO_EVIDENCE`) and reports `TASK_UNCERTIFIED`; the line never emits `TASK_STATUS_UNKNOWN`,
which only hack-drop's own stage reports for a task missing from a certified-tasks manifest.
`task_action` controls review versus drop for weak
tasks. The optional `task_policy` maps each status (`certified`, `provisional`,
`weak`, `uncertified`) to `pass`, `review` or `drop`; the defaults match today's
behavior (weak follows `task_action`, everything else passes), so a training
export can later require certified tasks by setting `"uncertified": "review"`.
Setting both `task_action` and a different `task_policy.weak` is refused. Missing
evidence is reported as `uncertified`, not promoted to certified.

`labels_policy.attempted_cheat` must explicitly be `exclude`, `cheat` or `clean`.
Attempted-but-failed cheats remain separately counted whichever policy is used.
All labels pass through the shared `hack_drop.load_labels` loader.

For `cached` AutoReview, supply `autoreview.cache` as an existing hack-drop
output manifest or directory containing `kept.jsonl`, `dropped.jsonl` and
`review.jsonl` with stored AutoReview stage verdicts. Every run that needs a model reading
needs a stored one; the line does not fill gaps with a live model call.

A stored verdict is a reading only if its reason codes say so: rows whose reasons are
`AUTOREVIEW_NOT_ELIGIBLE`, `AUTOREVIEW_DISABLED` or `AUTOREVIEW_CACHE_MISS` never are, whatever
details they carry. The current L0 decides eligibility. If it says a run is ineligible, any
stored reading is ignored and the run records `AUTOREVIEW_NOT_ELIGIBLE`, so a stale reading
never applies. If it says the run is eligible but the cache holds no actual reading, the run
is never passed silently: it goes to `review` with `AUTOREVIEW_CACHE_MISS`, and the
scoreboard reports `autoreview.cache_misses`, so a measurement of an L0 change cannot hide
runs the cache could not judge. A line export containing cache misses can be reused as a
cache; its misses stay misses.

The cache is untrusted input. Only the AutoReview stage-verdict shape is
accepted: decision, reasons from the AutoReview reason set, cited steps and a
whitelisted details object (suspicion, verdict, model, attested, cost, and the
eligibility and error notes). Any evaluator or label field anywhere in a cached
row, a free-form reason, an unexpected field, a verdict that contradicts its own
suspicion or attestation, or a non-DEV `split` stops the run before any batch,
queue or export is written. Details are never copied wholesale into
`trace.jsonl`. Each cached row's model is recorded as the route used and must be
in `models.allowed`; an unattested cached call fails the spend/model check.

Cached verdicts retain their historical model/score/evidence details. Reusing
those bytes makes no new model call: this batch records zero spend. Historical
costs are not charged a second time.

For `live` AutoReview set `autoreview.max_call_usd`, a per-call bound in
`(0, max_usd]`. Before each call the line reserves that bound and refuses the
call if spent plus the bound would pass `max_usd`; a call that costs more than
its bound stops all later calls and fails the spend check. This is a preventive
reservation at the line's call boundary, not a provider-side hard stop.

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
   cached AutoReview verdicts. The line does not invent a second detector. An exception in any
   detector the L0 stage reads makes the run `L0_PARSE_ERROR` review, never a pass; diagnostic
   sections no L0 decision reads are not run, so a fault inside one cannot change a decision.
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
(see the example spec) selects a separate Station export/state file, useful for
isolated local work. It is an extension of the contract's spec schema.
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

Receipts name the batch's own files relative to the batch directory (`tasks.jsonl`,
`export/kept.jsonl`); inputs outside the batch stay absolute with their SHA-256. A byte
copy of a batch therefore passes `line check` from its new location, and tampering with
the copy fails. Batches written before this change recorded absolute paths: they still
verify in place, but a copy of one fails with a plain "another location" message and
must be regenerated.

`line check` reads a closed set of files.

- **Batch.** The line never writes a symlink or a hard link into a batch, so a batch
  containing either is refused before anything else is read (a copy made with `cp -R` has
  neither). A stage receipt may list only that stage's own batch files (plus
  `evidence/station-inputs/*.jsonl` for the judge), and every output ref must resolve inside
  the batch.
- **Complete receipts.** Each stage receipt lists exactly the outside inputs the spec
  declares for it, derived from the spec and from an intake re-run from it, never from the
  batch's own `runs.jsonl`: intake lists the source manifests and every rollout; task lists
  the task-status file; trace lists the cache files; scoreboard lists the labels files; the
  other stages list none. An omission and an extra both fail provenance. Every listed input
  also passes the DEV gate.
- **Rollout admission.** At run time and at check time every rollout (a trial folder or a
  file) goes through `rollout_admission.admit_rollout` on its canonical path (`realpath`),
  once, together with the manifest row that listed it (its `id` is the run id, its fields are
  documents of the rollout). A manifest row must have a `path`: inline rollouts (a row with
  `steps` or only an id) are not admitted by the line, and a non-object row is refused. That
  one walk refuses any symlink, FIFO, socket or device, a hard link to sealed
  material, and a non-ASCII or case-ambiguous name; it opens each file without following
  links and only if it is still the file the walk saw; it hashes what it read and parses
  **every `.json` file** in the tree (trajectory, sidecars, `metadata.json`, verifier output,
  `sub/submission.json`, ...) and the sidecars of every ancestor folder: duplicate keys and
  malformed JSON are refused, a `split` key in any case at any depth (agent-written steps
  included) must be `dev`, every task and trial name any of them declares goes through the
  TEST-identity checks, and no key of `LABEL_KEYS` may appear. Two exclusions only, both for
  the label check: the value of a top-level `steps` list of a trajectory file (what the agent
  wrote and its tools returned) and parameter names under a `properties` mapping of a parameter
  schema inside a top-level `tools` list (a tool may have a parameter called `label`). The
  split check has no exclusion at all, so a tool parameter named `split`
  refuses the trial (fail closed; none of the 738 honest DEV runs checked carries one). A
  file nested deeper than 64 levels is refused. The rollout that is scored
  is built from those bytes (trajectory, sidecars, diff and bundle source files), and the
  hash recorded is that read's: no rollout file is opened again, and the tree hash equals the
  hash earlier batches recorded. A file inside a bundle that is another name for a label file
  is refused by label isolation. Hard links to anything else are allowed (native Harbor
  trials share files that way).
- **One reader.** Every declared input (spec, manifests, labels, task status, cache, Station
  verdicts, the Judge queue) goes through one reader, in `run`, `check`, `scoreboard` and
  `replay` alike: a final symlink is refused before the path is resolved, then a no-follow
  open, the open descriptor must be a regular file, must not be another name for a file under
  `research/answer-keys` (by device and inode), and is bounded at 512 MB with a plain error. The
  loaders parse the bytes that reader returns, and provenance hashes the same kind of read; a
  file whose bytes differ between two reads inside one check, by whichever name (hard-link
  alias or case variant: reads are keyed by inode), fails provenance ("changed while being
  checked"). The stored `spec.json` must equal what `load_spec` resolves it to now, so a
  declared input that has since become, or passes through, a link is refused.
- **No shared files.** A spec is refused, at run and at check time, when two declared inputs
  of different kinds (labels, manifests, task status, cache, judge queue, judge verdicts)
  are the same file by (device, inode) or case-folded real path; repeating one file within a
  kind, such as a labels file shared by two sources, is fine. Label isolation also compares
  the label files with what the spec and intake feed the decision stages, not only with what
  the receipts list.
- The scoreboard's row counting refuses paths that leave the batch.

## Eight quality checks

| Check | What must hold |
|---|---|
| Conservation | Every intake run has exactly one decision; keep + drop + review equals intake; audits are a subset of keeps; each export file equals its decisions exactly. |
| DEV only | Sources, runs and tasks pass the existing DEV gates; no TEST or answer-key paths (the path as spelled and its symlink target are both judged). A `split` other than exactly `dev` (any key case, any depth, the agent-written steps included) is refused wherever it is declared: manifest rows, every `.json` file of a rollout, task-status files (top level and rows), cached AutoReview rows and Station rows. A JSON list is refused as a manifest, and label files are validated (before any directory, queue or export is written, and again by this check). |
| Provenance | Receipt input/output hashes match disk and the code revision is recorded. Every stage receipt must list its expected files (for example the export receipt lists all three export files, the judge receipt lists a Station snapshot), so emptying a receipt does not hide a change. |
| Determinism | `line run` computes the decisions twice in-process and the two hashes must match; its closing check reuses those two passes. A later `line check` recomputes the decisions once and compares them with the stored decisions and both recorded hashes, and recomputes `trace.jsonl` (L0 and AutoReview evidence) from the inputs. |
| Spend and models | Spend stays inside the spec cap. Every AutoReview call records the route that actually served it and its fallback count in `trace.jsonl`; the route must equal the requested one, have zero fallbacks and be in `models.allowed`, and the trace receipt's model list must equal the routes in the trace. In live mode each call's cost stays within `max_call_usd`, and calls sum to the receipt spend. |
| Judge routing | Every review and audit appears exactly once in the local queue; no other run appears. |
| Label isolation | No label or evaluator field (`label`, `is_cheat`, `ground_truth`, `expected_verdict`, `attempted_cheat`, `hack`, ...) appears, at any depth, in runs, tasks, trace, decisions, the queue, the exports or any receipt; trace AutoReview details match the stage schema; label loading belongs to the scoreboard stage. |
| Scoreboard recompute | Re-deriving numbers from persisted decisions and shared-loader labels reproduces the scoreboard. |

Any failed check blocks a successful run/check exit. These are evidence and
consistency gates, not a claim that a detector has perfect recall or that an
export is automatically suitable for training.

`line check` first validates the batch directory and compares the saved `SCOREBOARD.md` with the
view rendered from the saved `scoreboard.json` (the result is carried to the
`scoreboard_recompute` gate); only then does it overwrite `check.json` with a not-passed,
all-`PENDING` result (and the scoreboard's quality-check list and view with the same), then
replaces it with the completed result. A missing, linked or tampered directory is refused before
any file in it is rewritten, so a hard link is never silently replaced by a fresh file. A crash
therefore leaves `passed: false`, never a stale pass. All JSON artifacts are written
atomically.

`line check --fast` skips the determinism recompute (the slowest part). It reports that gate as
`SKIPPED` with "determinism not verified", sets `determinism_verified: false`, never counts as
a pass (exit 3 when nothing failed, 2 when a check failed), and does not touch `check.json` or
the scoreboard. Use it for a quick look at a batch; the replay gate and every acceptance claim
use the full check.
The Station verdict file is read once, validated and copied into `evidence/station-inputs/`
when the batch runs, and the judge receipt names that immutable copy, so a Station file that
grows afterwards cannot break provenance. `verdicts.jsonl` is recomputed from that copy by
the judge-routing check. Task statuses use the line's vocabulary: hack-drop's
`gold`/`hardened` mean `certified` and `exploitable` means `weak`.

## Run order and side effects

`line run` refuses everything it can before a batch directory exists or the shared Judge
queue is touched: the spec and every declared input (DEV gate, duplicate JSON keys, malformed
or non-object label and manifest rows, shared files), every rollout (admission, see above),
the Station verdict file (invalid UTF-8, non-object rows, repeated keys, a split other than
dev, a label field; a line that is not JSON is skipped, as the Station does), and, when no
paid call is involved, stored AutoReview readings on a route outside `models.allowed` (or
unattested). Live AutoReview calls happen as the batch is built, so a refusal after them
still leaves the evidence on disk. The external Judge queue is read first, with the other inputs
(no link followed, never sealed material, every row an object with a `run_id`, no run of this
batch twice in it), and appended **last**, only after all eight checks passed on the finished
batch, and the external queue is then verified; a batch whose checks fail is left on disk with
`passed: false` and the queue is untouched.

## What covers each batch file

Every file `line check` accepts is covered either by a hash in provenance, by recomputation
from the declared inputs, or both. A batch may contain only these files (anything else, any
symlink, hard link, FIFO, socket or device, or a non-regular entry is refused before any
gate runs).

| File | Hash in a receipt | Recomputed or verified by |
|---|---|---|
| `spec.json` | intake receipt | must equal what `load_spec` resolves it to now; DEV gate |
| `runs.jsonl` | intake receipt | equals a fresh intake run from the spec (DEV only) |
| `tasks.jsonl` | task receipt | exact row-for-row recomputation from the task-status file (DEV only) |
| `trace.jsonl` | trace receipt | recomputed (off/cached); in live mode each stored reading must have the exact trace-row fields, `stage`/`mode`/`score`/`cited_steps` that follow from the reading, the configured route, a call record that agrees with it, and an outcome (not eligible, budget spent, no trajectory, error, reading) that the recomputed L0, the trajectory and the spend of the rows before it allow |
| `decisions.jsonl` | decisions receipt | recomputed twice and compared with the saved decision hashes |
| `judge-queue.jsonl` | judge receipt | recomputed from the decisions |
| `verdicts.jsonl` | judge receipt | recomputed from the pinned Station snapshot and the admitted runs; every row is checked for a split other than dev and for label fields outside the Station's own reveal blocks |
| `evidence/station-inputs/*.jsonl` | judge receipt | the snapshot the verdicts are recomputed from; the folder holds exactly the files the judge receipt lists |
| `export/*.jsonl` | export receipt | equal the decisions, row for row |
| `receipts/*.json` | not applicable | exactly the seven stages with a fixed set of fields; one `code_rev` and one `code_dirty` (a bool); the batch id; every input exactly `{path, sha256}` and every output exactly `{path, sha256, rows}`, no file listed twice; row totals recomputed from the files; trace errors recomputed from the trace; models and spend only on the trace receipt |
| `scoreboard.json` | scoreboard receipt lists the labels | recomputed from the batch and the labels, on its own schema version |
| `SCOREBOARD.md` | not applicable | rendered from `scoreboard.json` and compared (schema version 3 and later); `line check` compares the saved file BEFORE it rewrites it, so a tampered or deleted view fails instead of being repaired |
| `check.json` | not applicable | is the check output; its saved decision hashes are compared |

## Threat model

`line check` detects accidental corruption, stale or mixed-code batches, and edits to batch
files or declared inputs (a changed byte in a manifest, labels, task-status, cache or rollout
file, a swapped receipt, a flipped decision, a forged total), and it refuses the filesystem
tricks a rollout or a declared input can carry: links, special files, another name for sealed
material, a file that changes between two reads, a TEST declaration or evaluator field in any
JSON file of a rollout or in a Station row.

It checks consistency, not authenticity. Anyone who can write the batch **and** recompute
every hash, or change the code, can forge a batch that passes; nothing is signed. What is
left, exactly:

- **Live model readings.** A reading made by a live model call cannot be recomputed. `line
  check` requires every reading to have the exact trace-row shape, the configured route, a call
  record that agrees with it, and an outcome the walk of the budget, the trajectory and the
  recomputed L0 allows (a run for which the walk required a refusal can only be an error row,
  and a refused call has no call record), but a self-consistent reading forged with a matching
  call record is accepted. Re-reading it needs the model again. A stored (cached) outcome that
  is not a reading (no trajectory, budget exceeded, cache miss, error) carries one reason and
  no score, verdict, model or cost. `models.allowed` may not list `unattested`.
- **Sealed material is one folder.** "Another name for sealed material" means a hard link to a
  file under `<repo>/research/answer-keys` of the repository the command runs in. A label file
  elsewhere is kept out of the decisions by label isolation (compared by inode with every file
  admitted), not by being sealed.
- **Parents of an input.** A declared input (the spec itself, a manifest, labels, task status,
  cache, Station verdicts, the Judge queue) that is itself a symlink is refused before its
  path is resolved, and batches are opened without following a final link; a symlinked parent
  directory on the way is followed. A rollout path is `realpath`ed before admission, which
  then judges that path, so a link to a forbidden folder is judged by its target.
- **Only `.json` files of a rollout are parsed.** Other files (logs, `.jsonl` event streams,
  source files) are hashed and, if they are bundle source files, scored, but a `split` or label
  word inside them is not looked for. The sidecars of every ancestor folder of a rollout are
  read like its own files (no link followed, bounded, never sealed, split and label checked,
  inodes recorded); other files in a job folder next to the trials are not read at all.
- **Evaluator fields in manifests and task status.** Label fields are refused in every JSON
  file of a rollout and its ancestors' sidecars, in Station rows and AutoReview cache rows, and
  in everything the line writes. A manifest row or task-status file that carries one is
  accepted and the field is stripped on output, not refused (the row's `split` and its task
  and trial names are checked). A manifest row without a `path` (an inline rollout) is refused.
- **Station rows.** `revealed`, `reveal` and `detector_agreement` in a Station row hold the
  label the Station showed after the verdict, by design; they are copied into `verdicts.jsonl`
  and not scanned. A Station line that is not JSON (a torn last write) is skipped, as the
  Station does, and rows of runs the batch did not admit are copied into the snapshot with
  their identities unchecked (their split and label fields are checked).
- **Receipts.** Their shape, totals, hashes and the single `code_rev`/`code_dirty` are checked.
  Their timestamps and the revision text cannot be verified: a writer who recomputes them
  passes.
- **Memory.** Each file the line reads whole is bounded (512 MB); the number of files and the
  streamed hash of the others are not.

## Reading the scoreboard

`scoreboard.json` uses `evallab.line_scoreboard/v1` and records an integer `schema_version`
(3 now; a file without the field is version 1, the shape written before the field and the
`autoreview` section existed; version 2 added the field and section; version 3 marks batches
whose `SCOREBOARD.md` is rendered from the saved file, which `line check` compares too).
`line check` recomputes and compares a saved scoreboard
only on the sections its own version defines, so a batch built before a section was added
still passes, a tampered counted field still fails, and a version newer than the code
reads is refused with a plain message. `SCOREBOARD.md` is its
readable view. Overall and per-corpus summaries show total and labelled runs,
cheats, clean runs, attempted cheats and keep/drop/review counts. Each measured
rate carries numerator `k`, denominator `n`, a rate and a Wilson 95% interval:

- **Drop recall:** labelled cheats dropped / labelled cheats.
- **Drop-or-review recall:** labelled cheats dropped or reviewed / labelled cheats.
- **Clean false-alarm rate:** labelled clean runs dropped / labelled clean runs.
- **Clean drop-or-review rate:** labelled clean runs dropped or reviewed / labelled clean runs.
- **Drop precision:** labelled cheats dropped / all labelled runs dropped.

The scoreboard also shows stage rows in/out, seconds and errors; review-plus-audit
load per 100 runs; runs on each task status; reward-1 runs on every task status
(`reward1_certified`, `reward1_provisional`, `reward1_weak`, `reward1_uncertified`);
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

The gate compares **each corpus** and then **all corpora pooled** (when at least two have
the denominator). A change of at most `max(1 run, n / divisor)` runs passes, where `n` is
the metric's denominator:

| Metric | Bad direction | Per corpus | Pooled |
|---|---|---|---|
| Drop-or-review recall | falls | 2 points | 1 point |
| Drop recall | falls | 5 points | 2 points |
| Clean false-alarm rate (clean drops) | rises | 1 point | not gated |
| Clean drop-or-review rate (clean reviews included) | rises | 5 points | 2 points |

Any failed quality check is a regression, even if measured rates improve. A corpus that
the baseline has but the run lacks is a regression (`kind: corpus_missing`), not an invalid
comparison; a corpus the baseline lacks is invalid.

Why these gates exist: drop-or-review recall alone lets every drop quietly become a review
(drop recall is gated), lets "send everything to review" score 100% recall with no clean
drops (the clean drop-or-review rate is gated, so extra judge load is bounded), and lets
several corpora each slip within their own allowance (the pooled rows). Drop recall and
review load have the looser 5-point per-corpus allowance because moving a cheat from drop
to review is cautious rather than a miss and a detector that reads more formats may
legitimately shift runs between those two decisions; the pooled rows are tighter than the sum
of the per-corpus allowances. Honest runs that stay within the review allowance are still
listed under `informational` as `{corpus, metric: "clean_drop_or_review_rate", before_k,
after_k, denominator}`. Fewer reviews or higher recall than the baseline never fail.

A change exactly at the allowance passes. A zero denominator has no rate to
compare, and a declared corpus with no rows can be baselined and replayed.
Replay never reports "no changes" by silence. `changed_runs` lists corpus, run
ID and before/after decisions after a real comparison, and is an empty list only
when nothing changed. It is `null` with `changed_runs_unavailable` set when no
comparison ran, for example when writing a baseline, or when
`changed decisions unavailable: frozen inputs mismatch` because the fresh batch
could not be built. If frozen inputs or the spec changed, `inputs_changed` is
`true`, the $0 batch still runs and its changed runs are reported, and the exit
is 2. Exit codes are **0 pass**, **1 regression**, **2 invalid
input/comparison**. Ordinary replay never creates or refreshes a missing
baseline implicitly.

## Standard replay set

Use **`replay-v1`** for every detector change instead of inventing a new baseline.
It freezes the first 690 labelled DEV runs: Terminal Wrench (304), CheatBench
(150), Goodfire (150), C1–C5 (30), and Sol/Astra (56). All source manifests,
trajectories and sidecars, labels and stored AutoReview readings are copied to
`~/Developer/eval-lab-results/line/replay-v1/`, outside Git. The spec uses zero
spend, an allow-list naming only the stored readings' model
(`google-antigravity/gemini-3.8-flash`), no task-status evidence and an empty
frozen Station verdict export; replay never touches the real Judge Station queue
or state.

`MANIFEST.json` records SHA-256 and size for all 1,198 input files, including
`spec.json`. `baseline.json` is the line's baseline scoreboard and decisions;
`baseline-provenance.json` records its code revision, hashes, authorizer and reason.
The set's README explains its source and limitations.

**The set is pinned in git.** `src/evallab/line/replay_sets.json` records the SHA-256 of
the set's `MANIFEST.json`, `spec.json` and `baseline.json` and the code revision that
wrote the baseline. The script refuses (exit 2) when any of them differs, when
`baseline-provenance.json` is missing or disagrees with the pin (hashes, code revision,
a clean checkout, authorizer, reason), when the set holds a file the manifest does not
list or any symlink, or when a spec path (rollout manifests and their rows, labels,
cache, task status, Station verdicts, output folders) does not resolve inside the set,
compared after resolving symlinks and `..`. A frozen set therefore cannot be edited, and
a baseline cannot be replaced, without a pull request that changes the pin.

```bash
make line-replay        # same as: uv run python scripts/line-replay.py
# Another pinned set (its absolute spec paths must point inside it):
EVALLAB_LINE_REPLAY_SET=/absolute/path/to/replay-v2 \
  uv run python scripts/line-replay.py --name replay-v2
```

`EVALLAB_LINE_REPLAY_SET` defaults to the location above; `--set PATH` overrides
it. The spec stores absolute paths, so a copy of a set is refused rather than silently
replaying the original; a set is used where its spec says it lives. The wrapper prints
per-corpus drop-or-review recall and clean drops before versus after, the regression
reasons, the number of changed decisions (or why they are unavailable), and the verdict.
A full run took about 29 minutes of compute on 2026-11-02 and each run leaves a new
batch under `batches/`; run it through the machine's heavy-job lock if one is in force.
Changed decisions are written to `changed-decisions.json` in that batch, whose path is
printed. Recall includes unreadable runs routed to review; that alone is not evidence of
a real cheat detection. This is a DEV regression set, not a held-out detector-quality
claim.

Exit codes: **0 pass / 1 regression / 2 invalid** (a broken or tampered set, bad replay
input, or a summary that could not be produced). **77 SKIP** is returned only when the set
directory itself is absent, and prints that nothing was checked; an existing but incomplete
set is exit 2. CI without the private set reports the skip separately; the synthetic replay
tests that run the real replay on tiny sets still run normally.

**Baseline refresh rule:** only Peter or the assembly-line coordinator acting
for him may authorize a refresh, with the reason recorded. Detector authors
must first inspect the comparison and changed decisions, not overwrite the
baseline to turn a failure green. A refresh is a pull request: archive the old
baseline and provenance under `baseline-history/<UTC timestamp>/`, run the explicit
`evallab line replay ... --write-baseline` command from a clean merged-main checkout,
record the authorizer, reason, merged code revision, replay batch and baseline, spec and
manifest hashes in `baseline-provenance.json`, and update `replay_sets.json` in the same
PR with the new hashes and code revision. The PR description states the reason and
authorization. The initial baseline
was written explicitly on 2026-11-02 from merged main `d711f652e` (#1815): keep /
drop / review 100 / 252 / 338, 302 of 332 cheats dropped or reviewed, 40 of 358
honest runs dropped. Only 250 of those 302 are real detections; 52 reach review
because L0 cannot read their run format. Changed inputs, cohorts, labels or
policies need a new replay-set version, not an in-place edit.

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
