---
status: living
audience:
  - operator
  - analyst
---

# Harbor viewer as the results surface (HAR-170)

```bash
evallab view ~/Developer/eval-lab-results/2026-10-01/HAR-126-ovn-g5-* --out /private/tmp/har170-view
evallab view <job-dir> --no-launch   # build only, print the root
evallab view <roots...> --merge g5-all='HAR-126-ovn-g5-*'   # fold many jobs into one
```

`--merge NAME=GLOB` is repeatable; the glob matches source job dir names and
matched jobs are consumed into the merged viewer job. `--arm-regex` (a regex
with a named `(?P<arm>...)` group, defaulting to G5 `stock|tuned|gepa`
suffixes) labels each source job's arm; jobs without a hit keep their full
job name as the arm.

The repository pins Harbor 0.24. When a stale environment has an older version,
the viewer is launched via `uv tool run --from harbor==0.24.0 harbor view …`,
which resolves from the local uv cache offline once fetched. The 0.21 viewer
has no `chart-trials`, hence no Outcomes or Pareto; 0.24 is required.

## Always-on results viewer

`evallab view` is a snapshot: it serves the jobs named at launch, from a
fresh temp root. `evallab results-viewer` keeps one persistent root in step
with the results home and serves it at **<http://127.0.0.1:8100>**:

```bash
scripts/ops/launchd/install-results-viewer.sh --load   # install / upgrade the LaunchAgent
evallab results-viewer --once                          # one sync pass, print the summary
```

- Every `--interval` (60 s) a pass mirrors new jobs with the same overlay as
  `evallab view`, rebuilds jobs whose `result.json` changed (republished)
  and drops jobs whose source is gone. Discovery goes two levels below each
  source, so `<date>/<job>/` in the results home is found.
- A job is mirrored only once its tree has been quiet for `--settle` (60 s):
  `process-job` publishes by copying in place. Each viewer job is staged
  beside the root and renamed in, so the viewer never lists a partial job.
- The root is the state. Each viewer job carries
  `.evallab-results-viewer.json` (source path, source signature), so a
  restart resumes without rebuilding. A first pass over ~3,000 jobs takes
  about 4.5 minutes; newest jobs are mirrored first and appear as they land.
- Read-only by construction: only GET/HEAD with a loopback `Host` reach
  Harbor. The 0.24 viewer's run launcher, Analyze/Summarize, Hub upload and
  Delete buttons answer 405, and a DNS-rebinding page gets 403.
- `--merge` and task-health selection stay in `evallab view`; the always-on
  root holds one viewer job per published job.
- The LaunchAgent runs a non-editable snapshot installed under
  `~/Library/Application Support/evallab/results-viewer/venv`, not a
  worktree. Re-run the installer from an updated checkout to upgrade it;
  `INSTALLED` beside the venv records the commit. Logs:
  `~/Library/Logs/evallab/com.petermakhnatch.evallab.results-viewer.{out,err}`.

## Task pages (`task-<id>`) and trace readers (HAR-176)

Each pass of the always-on viewer also keeps one page per task. Open
`task-000792` at http://127.0.0.1:8100 to see every trial of that task from the
trusted jobs (`HAR-168-*` by default; repeat `--trusted-job GLOB` to change it).

- **Trial list.** Each trial shows its reward dims plus flag dims (`1` means
  flagged): `copied.copy_check`, `copied.rewardkit`, `copied.laminar`,
  `reward_hacking.analyze`, `stuck_loop.laminar`, `false_completion.laminar` and
  `task_spec.analyze`. The attempt (`a1`, `a2`, …) is the dataset column.
- **Job Analysis tab.** One card per trial: raw/gated reward, stop reason, task
  health tags, a `harbor check` summary, watch alerts, and a pass/fail badge for
  every source and check.
- **Trial Analysis tab** (`analysis.md`). A clickable **Open this trial in
  Laminar** link, a "Copied?" table with every source's verdict and evidence, the
  other reader checks, the task's `harbor check` table and the watch alerts.

Reader verdicts live in `~/Library/Application Support/evallab/readers`
(`$EVALLAB_READERS_STORE`, or `--readers-store`) as
`<job>/<trial>/<reader>.json` (`evallab.reader_verdict/v1`). Task checks are at
`_tasks/<task>/harbor_check.json`. A page is rebuilt when a member trial, a
verdict or the task's `task-health-tags@1` record changes.

| Reader | How it runs | Copy recall (H12 / copy benchmark) | False positives |
|---|---|---|---|
| Laminar Signal `copied_upstream_fix` | Automatic: Laminar Cloud on every trace (Z.ai `glm-5.3-flash`, about $0.013 per trial for the 3 kept Signals). The viewer pulls verdicts every 5 min when `LMNR_PROJECT_API_KEY` is set; the installer runs it under `keys run --`. | 2/2, 8/8 | 0/9, 0/19 |
| `harbor analyze` (default rubric) | Manual: `python -m evallab.readers.harbor_analyze analyze <trial>` (mini-swe-agent, `glm-5.3-flash`, about $0.018 per trial, about 3 min) | 1/2, 1/8 | 0/8, 0/5 |
| `harbor check` | Manual, once per task: `... harbor_analyze check <task> --task-name <name>` (about $0.032 per task) | n/a | n/a |

- **Analyze inputs are blinded.** Its copy omits Eval Lab labels (`integrity`,
  `reward_gated`, `reward-details.json`, processed/watch files) and our integrity
  machinery. Before that, it simply re-ran our own copy detector shipped in the
  task's grading files. Only verdicts with
  `input_policy=evallab.reader_input/blind-v2` are shown.
- **Dropped readers.** Inspect Scout with METR's `reward_hacking` / `broken_env`
  scanners caught 0/2 copied H12 passes and 1/5 copy-benchmark passes. Those
  numbers come from an adapted, not native, setup, and it was dropped. The
  `infra_not_model` Signal is paused: it found 0/5 labelled infra failures, so
  infra stays on the watch's rules.

## Filter historical jobs by task health

```bash
evallab tasks health-tags \
  --variants-root derived/task-health/variants \
  --view-root derived/task-health/view \
  --output derived/task-health/manifest.json
evallab view ~/Developer/eval-lab-results/2026-10-02/HAR-157-har157-mimo-base-* \
  --task-health derived/task-health/manifest.json \
  --tag health:sound --tag solve:never-run
evallab view ~/Developer/eval-lab-results/2026-10-02/HAR-157-har157-mimo-base-* \
  --task-health derived/task-health/manifest.json --tag solve:mixed \
  --merge 'har157-solve-mixed=HAR-157-*' \
  --arm-regex '(?P<arm>har157-mimo-base)'
```

This is selection in the existing viewer **launcher**, not a new frontend or
an unsupported native jobs-mode tag widget. Harbor renders the selected jobs,
trials, Outcomes and Pareto as usual. Browse task metadata itself with
`harbor view derived/task-health/view --tasks`.

`--task-health` and `--tag` must be supplied together. Repeated tags are
**ANDed**. Selection happens **before** mirroring or merging. Jobs without
selected trials disappear, and config task/model facets and job-result
aggregates are rebuilt from only the selected trials. Original whole-job
`analysis.json` is not reused for a smaller cohort. A valid empty selection
builds an empty report and does not launch a viewer.

The join is by retained **package identity**, never a job/task-name suffix:
trial config path → matching native trial lock (or a unique task digest in
the job lock) → manifest parent/variant Harbor digest. When the Lab staging
sidecar binds that executed digest, its source-package digest identifies
the original ledger package. A contradictory verified source cannot fall
back to a favorable native match. A different task in a multi-task job
cannot inherit the sidecar's labels.

Derived packages such as HAR-169's `rewardkit-integrity@1` need not appear
directly in the health manifest. The filter follows strict `VariantRecord`
links (`variant_digest` → `parent.digest`) to the **nearest** manifest
identity, checking the full-package/Harbor digest pair at every parent.
This works across multiple transforms and when a record's parent source is
`local`; an old source path is not a lookup key. A nearer ancestor's tags
always win, even when they exclude the trial.

The launcher includes `library/task-variants` under its repository root
when present. For canonical records retained elsewhere, add
`--task-variants runs/har168/task-variants` (repeatable for additional trees).
Programmatic callers pass `records_dirs` to `TaskHealthFilter`, or
`variant_records_dirs` to `build_viewer_root`; neither implicitly scans the
process's working directory. Missing links, digest-pair mismatches and cycles
are excluded; conflicting identities are ambiguous. Identical copied
records do not create false ambiguity. Malformed record inputs fail closed.
Inherited tags are evidence about the manifest ancestor, **not** independent
health validation or admission of the derived verifier/package.

The CLI reports included, tag-mismatch, unbound, digest-mismatch and ambiguous
counts. `.evallab-view.json` retains the manifest hash and every trial's join
or exclusion reason. Transitive bindings include the leaf-to-ancestor
`binding.lineage`: each record path/hash, transform and parent/child digests.
Missing identity is **not** labeled healthy. All
original config, lock, result, trajectory and provenance bytes stay untouched.
Task-health labels are the manifest's evidence snapshot; historical
`solve:*` counts retain their documented clean-pass/fail denominator, not a
new current-package capability estimate.

## Reward dims for jobs that lack them

New runs get `reward` / `integrity` / `reward_gated` natively from RewardKit
inside the verifier (HAR-169). Existing jobs predate that, so `evallab view`
backfills the same dims in overlay trial dirs. Sources are mirrored without
copying bytes: directories are recreated and files hard-linked (a symlink
only across filesystems). Symlinks would not do: the 0.24 viewer refuses a
trial or file whose resolved path leaves its jobs root, so a symlinked trial
answers "Invalid trial name" and a symlinked file "Access denied".

- trials whose `verifier_result.rewards` already carry `integrity` are
  mirrored byte-identical (native RewardKit runs);
- unscored trials (no numeric `reward`) are mirrored with no dims invented;
- every other scored trial gets an overlay trial dir: every original child
  mirrored, except a rewritten `result.json` whose rewards add
  `integrity` (0/1) and `reward_gated` (`reward * integrity`) while keeping
  `reward` unchanged, plus a new `reward-details.json` with the fired rule
  ids, evidence, provenance, and rule versions.

Integrity rules are deterministic only (HAR-024 contract, no LLM judge):

| Rule id | Version | Detector (reused, not copied) |
|---|---|---|
| `evallab.copy_check` | `copy_check/v1` (`copy_check.RULE`) | `evallab.copy_check.copy_check`. Its outside-source matcher already subsumes the "build/lib / site-packages / out-of-base git read followed by matching lines" source rule. |
| `evallab.upstream_fetch` | `confirmed_fetch` (the strict evidence predicate) | `assess_upstream_fetch` + `confirmed_fetch`: attempts without confirmed acquisition evidence never fire. |
| `evallab.grader_tamper` | `v1` (new as a reward rule here) | `evallab.live_watch._grader_tamper_hits` (imported, not edited). |

Detector preference: the stored `<job>/processed/trial-<name>.json` taint
verdict is used for copy_check / upstream_fetch when present and trustworthy
(a recorded analysis error falls back to computing, since the stored taint
may be an incomplete `[]`). Grader tamper has no stored verdict anywhere and
is always computed; the per-rule `source` (`stored` / `computed`) is recorded
in `reward-details.json`. A `guard_reject` taint never fires integrity on its
own: the guard held, so no contamination happened. Detector errors fail open
(integrity 1) and are recorded under `problems`.

## Outcomes and Pareto: what each needs, what we supply

Read from `apps/viewer/app/lib/{pareto,waffle}.ts` and
`components/job-evals-{pareto,waffle}.tsx` (0.24):

- **Grouping keys**: `agent_name`, `model_name`, `source` (waffle rows/cols via
  `trialGroupKey`; pareto groups the same way). All come from the trial
  `result.json` natively.
- **Selectable reward dim**: every key of `verifier_result.rewards` becomes an
  eval (`evals_from_rewards`), so `integrity` and `reward_gated` are
  selectable in both tabs (Outcomes "COLOR BY", Pareto "vs" axis) with no
  viewer change. The primary `reward` stays the first key, unchanged.
- **Cost / tokens / time**: `compute_token_cost_totals` sums
  `agent_result.n_input_tokens / n_cache_tokens / n_output_tokens / cost_usd`
  (single-step) or per-step contexts (multi-step). Pareto needs one of cost /
  tokens / time plus a numeric reward; missing resource data omits the point
  ("1 point omitted due to incomplete cost or reward data") instead of
  averaging a different population.
- **Attempts**: `n_trials` / `n_completed` come from the scan; nothing extra
  is needed.

Nothing is fabricated: G5 trials already carry `agent_result` token counts
natively, so tokens/time Pareto axes work as-is. `cost_usd` is null for our
self-hosted trials (the route bills by server time, not tokens; the processed
`cost_estimate_usd` is a non-additive estimate, never a per-trial cost), so
the cost Pareto axis is honestly empty. Baseline v1 trials failed before any
model call (all `verifier_result` null), so there is nothing real to fill
from the proxy ledger or processed records, and no zeros are invented.

## Proof: G5 + Baseline v1 (`/private/tmp/har170-view`)

Build: 78 jobs (60 G5 + 18 baseline), 78 trials, 55 scored (all overlay,
0 native), 23 unscored, 6 with integrity 0. API sweep over all 78 jobs:
every scored trial's `chart-trials` evals are exactly
`{reward, integrity, reward_gated}` with `reward_gated == reward * integrity`;
every unscored trial has empty evals. Six trials have integrity 0, all on
reward-0.0 trials, so no gated outcome changed.

Integrity counts per job (rule in parentheses):

- `HAR-126-ovn-g5-000169-stock`: 1 (`evallab.grader_tamper`)
- `HAR-126-ovn-g5-000842-tuned`: 1 (`evallab.grader_tamper`)
- `HAR-126-ovn-g5-001797-gepa`: 1 (`evallab.grader_tamper`)
- `HAR-126-ovn-g5-001833-stock`: 1 (`evallab.grader_tamper`)
- `HAR-126-ovn-g5-001833-tuned`: 1 (`evallab.grader_tamper`)
- `HAR-126-ovn-g5-002302-gepa`: 1 (`evallab.grader_tamper`)
- All other 72 jobs: 0. No `evallab.copy_check` or `evallab.upstream_fetch`
  fires anywhere in G5 or Baseline v1.

The 5 unscored G5 trials are infra failures with no verifier output
(`DaytonaNotFoundError` ×3, `ServiceUnavailableError` ×2); the 18 Baseline
v1 trials are `RuntimeError` / 503 failures before scoring.

Screenshots (1600×1000, headless Chrome against the 0.24 viewer):

- `/private/tmp/har170-view/shots/g5-outcomes.png` — Outcomes for
  `HAR-126-ovn-g5-000169-tuned`, COLOR BY INTEGRITY selectable and working.
- `/private/tmp/har170-view/shots/g5-pareto-tokens.png` — Pareto
  TOKENS vs REWARD_GATED with a real point (terminus-2, ~2.6M tokens,
  mean reward_gated 1.0).
- `/private/tmp/har170-view/shots/g5-pareto.png` — Pareto COST default:
  empty state (no cost data on the self-hosted route; see below).
- `/private/tmp/har170-view/shots/blv1-outcomes.png` — Outcomes for
  `har157-mimo-base-000084`: grouping renders, the single cell is an
  Error cell (no reward to color by).
- `/private/tmp/har170-view/shots/blv1-pareto.png` — Pareto empty state:
  no numeric reward exists, so no axis can render.

## Merged viewer jobs (`/private/tmp/har170-merge`)

One Harbor job per trial makes per-job Outcomes/Pareto useless (one cell,
one point), so `--merge` folds many source jobs into one viewer job whose
trials are all their trials:

```bash
evallab view <G5 jobs...> <blv1 jobs...> --out /private/tmp/har170-merge --no-launch \
  --merge 'g5-all=HAR-126-ovn-g5-*' --merge 'blv1-base=har157-mimo-base-*'
```

Build: 2 jobs (`g5-all`: 60 trials, `blv1-base`: 18), same 55 scored /
23 unscored / 6 integrity-0 totals. `chart-trials` on `g5-all` returns 60
trials with `source` split exactly 20/20/20 across the arms.

Job-level files are synthesized from the real overlay trial docs, never
invented: `config.json` is the first source job's config with the merged
`job_name` and unioned `agents`/`datasets`/`tasks` (so the jobs list shows
both `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B` and the `:har129` model);
`result.json` aggregates `n_total_trials`, completed/error counts, per
`agent__model__dataset` reward/exception tallies, and token sums exactly as
`JobStats.increment` does. Job-level metric means are left empty (the
runner's means are not recomputed; trial-level evals are complete).
Every merged trial records `.evallab-source.json` (source job + arm), and
the manifest maps every trial to its arm.

### Arm grouping key: `source` (the dataset dimension)

Outcomes offers task / dataset / model / agent / reasoning-effort / provider
grouping (`waffleGroups`); Pareto groups by `agent__model__source`
(`trialGroupKey`). Of these, only `source` varies per arm without touching
`reward` or misstating the model: `-tuned` is already separable by model
(`...:har129`), but `-stock` vs `-gepa` share agent, model,
provider, and task, differing only in agent kwargs (prompt). So the merge
labels each overlay trial's `source` with its arm (a pre-existing non-null
source is preserved as `<source>+<arm>`), documented here and in
`reward-details.json` / `.evallab-source.json`. `reasoning effort` was
rejected (would misstate effort); task-name tricks were rejected (not a
viewer grouping key).

Merged screenshots (same 1600×1000 headless-Chrome setup):

- `shots/merged-g5-outcomes-integrity.png` — `g5-all` Outcomes, TRIALS BY
  TASK AND DATASET, COLOR BY INTEGRITY: 20 task rows × gepa/stock/tuned.
- `shots/merged-g5-outcomes-reward.png` — same grid COLOR BY REWARD_GATED.
- `shots/merged-g5-pareto.png` — TOKENS vs REWARD_GATED with three separate
  arm points (stock, gepa, tuned incl. the `:har129` model).
- `shots/merged-blv1-outcomes.png` — `blv1-base` Outcomes: 18 Error cells
  (arms fall back to full job names; nothing scored).
- `shots/merged-blv1-pareto.png` — empty state: "A numeric reward is needed
  for a Pareto chart."

Note: the `browser` global in eval was unusable (its relay reports out of
date), so the screenshots were taken with headless Chrome directly after two
failed `browser.open` attempts; they are real renders of the served viewer.

## Empty states and their causes

1. Pareto COST axis on G5: "No evaluations have complete cost data and a
   numeric value for this reward." Cause: `cost_usd` is null on self-hosted
   trials. TOKENS and TIME axes populate normally.
2. Baseline v1 Pareto (any axis): same empty shape. Cause: `verifier_result`
   is null (503s before scoring), so there is no numeric reward; tokens are
   also null (no model call ever ran). Nothing to fabricate.
3. Baseline v1 Outcomes: renders with an Error cell, COLOR BY REWARD only
   (no integrity option, correctly: there are no dims). Cause: same as (2).

## Retired vs remaining (compared 2026-10-05)

The viewer now covers: per-job trial listing with selectable reward dims,
Outcomes grouping, and Pareto frontiers — capabilities we never had. None of
our existing pages is fully covered, so nothing is deleted:

| Page | Generator | Verdict | Why |
|---|---|---|---|
| `INDEX.md` / `INDEX-all.md` publishing | `results_home.py` | remain | The viewer does not publish byte copies to the results home, record repository/provenance, or survive worktree retirement. |
| Processed trial/job pages (`trial-*.json/md`, `job.json/md`) | `process_job.py` | remain | Taint, counts verdicts, diagnosis, decisions, token flow: the viewer shows only `TrialSummary` (rewards, tokens, cost, exceptions). The stored taint verdicts are also what `evallab view` prefers. |
| Run report | `interpretation/run_report.py` | remain | Run-level interpretation with decisions; not a trial table. |
| Dashboard leaderboard | `dashboard/queries.py` | remain | SELECT-only catalog over derived stores with counts-verdict semantics; the viewer compare grid averages raw reward per task with no counts, ledger, or spend joins. |

The only retirements are the copy-paste commands: `dashboard/README.md` and
the two `harbor view` next-actions in `src/evallab/explorer.py` now point at
`evallab view`, which adds the dims before serving.

## Viewer limitations found

- Without `--merge`, single-trial jobs (all of G5/Baseline) render one-cell
  Outcomes and one-point Pareto charts; merged jobs fix that.
- Outcomes colors trials carrying `exception_info` as Error cells even when
  they scored (56/60 G5 trials hit trial-budget/infra exceptions and still
  verified). Dim values show on clean trials; Pareto means include
  errored-but-scored trials.
- Pareto COST is unusable for self-hosted runs (null `cost_usd`); use
  TOKENS or TIME.
- The viewer reads only `config.json` + `result.json`; `reward-details.json`
  evidence (fired rules, provenance) is one level down — visible in the
  overlay trial dir, not in the UI. Trial-level "why gated" still needs the
  processed pages above.
- `harbor view` also serves Run, Summarize, Upload and Delete endpoints.
  Deleting from an `evallab view` root only unlinks the root's names, but a
  hard-linked file shares its source's bytes, so nothing may write through
  a view root: do not use those buttons on one. The always-on results viewer
  refuses every non-GET request.
