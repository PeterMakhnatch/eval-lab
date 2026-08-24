Status: done
Last: merged as PR #69 (`30e6e0f`)
Next: none
Blockers: none

# DIGEST — stop the daily report from stating things that are not true

Worktree `.worktrees/digest-honesty`, branch `role/digest-honesty`, from
`origin/main` `0960eea`. Leased and touched: `src/evallab/digest.py`,
`src/evallab/researchers.py`, `tests/test_unattended.py`,
`docs/fleet-tracking.md`, this file. Nothing else — in particular not
`queue.py`, `cli.py`, `policy/`, `schemas.py`, `explorer.py`, or `dashboard/`.

Note on the brief: it states `origin/main` is `7456ac8`. At `git fetch origin`
it was `0960eea` — `7456ac8` (PR #66) plus one commit, `0960eea` "Build plan
(living)". This branch is off `0960eea`.

## The Fleet decision, and why

**Keep a fleet view; change what it claims. Do not remove it, and do not
restate `OWNERS.md`, `ACTIVE.md`, or `gh`.**

The block was not a role registry that went stale. It never was a registry: it
globbed `agents/handoffs/*.md`, upper-cased each filename into a `role` column,
and filled missing header fields with `unknown`. `adapter.md` therefore became
"a role named ADAPTER at status unknown". Nothing in the renderer ever knew
what a role was, so no amount of archiving could have made the column honest —
it was manufacturing an entity out of a filename.

That distinction decides the question. **Removal would delete a real
capability to fix a labelling defect.** The handoff files are the only
per-mission source that a worker updates itself at every stopping point, they
need no network, and the Sponsor has no other async view of what is running.

The two alternatives the brief names are worse than they look:

- **`agents/missions/ACTIVE.md`** is human-edited by the integrator alone, and
  it is measurably behind: at `origin/main` `0960eea` its "Now" section opens
  `origin/main` is `86380b0`, lists PR #51 as the only open PR, and knows
  nothing of the 18 PRs merged today. Copying it into a generated report would
  let a stale hand-written board arrive wearing the authority of a measurement.
  Restating `agents/OWNERS.md` is the opposite failure: four permanent lanes,
  identical every single day, which is noise, not news.
- **Open PRs** would be genuinely useful, and they are the one thing a handoff
  cannot self-report honestly. But the digest renderer makes zero subprocess
  and zero network calls today, `gh` is absent or unauthenticated in a nightly,
  and `agents/CHECKS.md`'s deterministic-test rule forbids tests that depend on
  a developer's credentials. That is a real feature with a real cost, not a
  line in this function. Recorded as a follow-up below, not smuggled in here.

So the block now reports exactly what it observes, and says so:

| old | new | why |
|---|---|---|
| `role` column, `ADAPTER` | `mission (handoff file)`, `adapter.md` | the renderer observes a file; it must not name an entity |
| missing header → `unknown` | named under "No machine-readable `Status:` header" | `unknown` asserts a role exists in an unknown state; the fact is that the file states nothing |
| `done` rows listed as fleet | counted as "Reported `done`, awaiting archive" | a finished mission has said it is not running |
| columns unlabelled | one sentence: self-reported at the last stopping point, not verified against branches, PRs, or CI | tells the reader the confidence of every cell |
| — | names `OWNERS.md` and `ACTIVE.md` without restating them | route without vouching |

**Same input, both renderers.** The proof that this was a code defect and not
an archiving lapse: point `agents/handoffs/` at the 39 files archived in
`agents/archive/2026-08-15-handoffs/` — the set behind the committed Fleet
block — and render it through `origin/main`'s `append_fleet_section` and this
branch's.

| | block lines | table rows | `ADAPTER` mentions | `\| unknown \|` cells | `\| done \|` rows |
|---|---:|---:|---:|---:|---:|
| `origin/main` | 60 | 39 | 1 | 4 | 8 |
| this branch | 54 | 27 | 0 | 0 | 0 |

27 + 8 reported `done` + 4 with no header = 39, one per input file. Nothing is
dropped silently; the two counts name every file they cover.

**The residual risk, stated rather than papered over.** An *open* handoff left
un-archived after its mission ends is still listed. I looked for an in-repo
signal and there is no honest cheap one: file mtime is checkout time in a fresh
clone, and a git-log probe would put a subprocess in a renderer that has none.
Archiving discipline remains the mechanism; the block now says the status is
self-reported so a reader knows what the claim rests on.

## The other four

2. **Queue events.** Runs of *consecutive* events identical in `(event, job,
   policy-or-reason)` collapse into one row with `×count` and a first–last time
   range. Identity deliberately includes the reason code, so
   `tick_deferred | executor_busy` is never absorbed into a run of
   `tick_deferred | no_approved_specs`; a run of one renders verbatim, which is
   what guarantees every distinct event keeps its own line. Over the measured
   day: the 58 `tick_deferred` events in the snapshot become 3 collapsed rows
   (`×2`, `×44`, `×10`) plus 2 `executor_busy` rows still listed individually.
   The committed digest saw 48 of them; the extra 10 are ticks the live log
   recorded after it was written.
3. **Canary drift.** `_drift_loader(period_date)` and `_drift_loader(report_date)`
   now yield `(day, observation)` pairs and the table leads with a `day` column,
   so the three "n=0 insufficient history" rows are dated 2026-08-15 and the
   three "n=3" rows 2026-08-16. `schemas.py` is another mission's lease, so the
   day is carried at the digest layer where it is already known, not added to
   `CanaryDriftObservation`.
4. **Completed trials — aggregated, not numbered.** One row per
   `(job, task, agent)` with a `trials` count and a `rewards` cell holding every
   recorded reward. I did **not** add an attempt number: the catalog records no
   attempt ordinal, only opaque Harbor trial names
   (`event-summary__5E3btLv`, `event-summary__EKfePmM`, …), and ordering by
   `finished_at` to synthesise "attempt 1..3" would invent a sequence nothing
   recorded — the same class of defect as the rest of this PR. The rewards are
   therefore **sorted, not sequenced**. `1 ×3` and `0, 1, 1` cannot be confused,
   and a partly failed job reads
   `3 | 1, 1, +1 unscored | NonZeroAgentExitCodeError (1 of 3)` — the failed
   attempt survives aggregation.
5. **Judge calibration.** The literal `not available until brief 09` is gone.
   `_judge_calibration_line` reads `research/calibration/records/`, validates
   each file as a `JudgeCalibrationRecord`, and reports the measured state.
   Today that is: **no judge is calibrated** — 0 of 1 measured record reaches
   its floor, closest `checkout-pool-exhaustion / harbor-codex-agent
   gpt-5.6-sol, mean agreement 0.763 against a 0.90 floor over 22 documents
   (2026-08-14)`. The line adds that no judged dimension is reportable and the
   analysis worker's `calibrated_judges_only` gate stays closed until one clears
   its floor — grounded in `src/evallab/analysis_worker.py:1002`
   (`"calibrated_judges_only": lambda: False,  # fail closed: no measured pass`)
   and its docstring at 972–974, which state the gate opens when a measured
   record meets the floor. The line is derived, so it changes by itself when
   that happens; it cannot become the next hardcoded placeholder.

## Before/after over the catalog day that produced `digests/2026-08-16.md`

Same inputs to both renderers: the live catalog read-only for
2026-08-15/2026-08-16 (58 and 9 trials; 3 and 3 drift observations), one
snapshot of `queue/events.jsonl` (144 lines), this worktree's
`research/calibration/records/`, `agents/handoffs/`, and `policy/`.
"before" is `origin/main`'s `digest.py` + `researchers.py` loaded from
`git show`; "after" is this branch. Measurement harness was throwaway under
gitignored `runs/_measure/` and has been removed.

**Total: 209 → 154 lines.** Line counts below are whole-section lines; row
counts are Markdown table data rows, header and rule excluded.

| section | committed file | before | after |
|---|---:|---:|---:|
| Completed trials | 62 (58 rows) | 17 (9 rows) | 13 (3 rows) |
| Early-morning automation | 15 (9 rows) | 15 (9 rows) | 11 (3 rows) |
| Canary drift | 10 (6 rows) | 10 (6 rows) | 12 (6 rows) |
| Evidence and calibration | 5 | 5 | 5 |
| Queue events | 91 (84 rows) | 101 (94 rows) | 50 (41 rows) |
| Fleet — Roles / Missions with a live handoff | 28 (24 rows) | 17 (13 rows) | 19 (13 rows) |
| *(unchanged)* Automation status / Cost and failures / Queue / Funnel / Discoveries | 5 / 4 / 7 / 7 / 5 | 5 / 4 / 7 / 7 / 4 | 5 / 4 / 7 / 7 / 4 |

Reading the three columns: the **committed file** predates #62, so its 62-line
Completed trials still holds the 49 smoke rows and its 28-line Roles table
holds the 24 pre-archive registry rows. **before** is `origin/main` today —
smoke already summarised by #62, and `agents/handoffs/` down to 13 files after
COORD-GC archived the rest (12 live missions plus this one), which is why its
Roles table is 17 lines rather than 28. Its Queue events is *larger* than the
committed file's (101 vs 91) because the live log kept ticking after the digest
was written. **after** is this branch on identical data.

Section by section, what a reader sees change:

- **Completed trials**: 9 rows → 3, one per canary job, each carrying
  `trials 3` and its reward spread. No row is byte-identical to another.
- **Early-morning automation**: 9 rows → 3. The transaction-reconciliation job
  now reads `3 | 1, 1, +1 unscored | NonZeroAgentExitCodeError (1 of 3)`
  instead of two identical reward-1 rows and one exception row.
- **Canary drift**: still 6 observations, now dated — three `2026-08-15` rows
  with `insufficient history, n=0` and three `2026-08-16` rows with `n=3`. The
  section grows by 2 lines (the day column plus a one-line lead-in) and stops
  showing three canaries contradicting themselves.
- **Evidence and calibration**: same 5 lines, one of them now true.
- **Queue events**: 94 rows → 41. 56 heartbeat events fold into 3 rows, and
  both `executor_busy` ticks are still listed individually.
- **Fleet**: **no row count changes on the current handoff set** — all 13 live
  files report an open status, so all 13 are still rows, and the block grows by
  2 lines for its provenance sentence. That is the honest result: today's
  directory contains no retired role to remove, because COORD-GC archived them.
  The defect was in the renderer, and the archived-set comparison above is where
  it shows: 39 rows → 27 rows plus 2 counted lines, `ADAPTER` and every
  `unknown` cell gone.

Nothing in the rendered output names a role, brief, or verdict that does not
exist: the roles are gone, "brief 09" is gone, and the only verdicts are the
per-day canary assessments and the derived calibration state.

## Tests and negative controls

Six new tests in `tests/test_unattended.py`. Each was run with its own fix
reverted; a control also records that the mutation left both modules importable
and, where the fix is isolated, that the other 25 tests in the file still pass.

| test | fix removed | mutated lines | module importable | target test | rest of file |
|---|---|---:|---|---|---|
| `test_fleet_reports_live_handoffs_and_never_a_retired_role` | role table restored (upper-case stem, `unknown` filler, no split) | 48 | yes | FAIL | 25 passed |
| `test_digest_collapses_a_repeat_run_but_never_a_distinct_event` | `_collapse_identical_runs` bypassed | 2 | yes | FAIL | 25 passed |
| `test_canary_drift_names_the_day_of_every_observation` | `day` column dropped | 2 | yes | FAIL | 25 passed |
| `test_digest_aggregates_repeated_trials_and_keeps_the_reward_spread` | `_group_trials` returns singletons | 4 | yes | FAIL | 25 passed |
| `test_digest_reports_the_measured_judge_calibration_state` | brief-09 literal restored | 2 | yes | FAIL | 24 passed, 1 failed (5b, same mutation) |
| `test_digest_says_no_judge_is_calibrated_when_a_record_clears_no_floor` | brief-09 literal restored | 2 | yes | FAIL | 24 passed, 1 failed (5a, same mutation) |

All six fail without their fix. 5a and 5b share one mutation, so each takes the
other down; that is the expected pairing, not a stray failure.

Two of the six are written specifically against the failure mode the brief
warns about rather than the happy path:

- the queue-events test interleaves `no_approved_specs` runs of 3 and 2 around
  a single `executor_busy` tick and asserts the exact three rendered rows, so a
  collapse that swallowed the distinct event would fail even though the row
  count still shrank;
- the trials test renders 1/1/0, 1/1/1, and 1/1/exception side by side and
  asserts all three render differently.

## Verification

- `uv run pytest` — **675 passed**.
- `uv run ruff check .` — All checks passed.
- Negative controls — `bash runs/_measure/negative_controls.sh`, output above;
  the harness itself was throwaway and is removed.
- Before/after render — throwaway harness under gitignored `runs/_measure/`,
  read-only against the shared catalog and the primary checkout; removed.
- No paid agent executed; no `codex` or `claude-code` invocation; `oracle` and
  `nop` were not needed either, since every measurement reads existing catalog
  rows. No scheduled job touched, no `launchctl`, no `docker compose`.
- No write to the primary checkout `~/Developer/eval-lab` other than the
  `git worktree add` the brief instructed.
- Shared catalog read-only and unchanged: **72 jobs, 23 `trajectory_documents`**
  before and after (`docker exec eval-lab-postgres-1 psql -U evallab -d evallab`).

## Reported, not fixed

- **Open-PR state in the Fleet block (Platform + Integration).** The one useful
  fleet fact a handoff cannot self-report. It needs a subprocess/network seam in
  a renderer that currently has none, plus an injected `gh` collaborator to stay
  inside `agents/CHECKS.md`'s deterministic-test rule, plus a decision about what
  the nightly does when `gh` is absent. A mission, not a line.
- **Fleet rows are unbounded in width.** `Last:`/`Next:` are rendered verbatim,
  and several current handoffs write 400–700 character sentences, so those table
  cells are effectively unreadable on a phone — which is the delivery channel
  `docs/fleet-tracking.md` describes. `origin/main` behaves identically, so this
  is not a regression, and I deliberately did not truncate: silently cutting a
  mission's own words is the same class of defect as the five fixed here. The
  honest fix is a length convention in `agents/WORKFLOW.md`'s handoff contract
  (Integration lane), enforced where handoffs are written, not where they are
  read.
- **`Cost and failures` and the Fleet funnel still report dollars.**
  `Recorded spend: $1.8378 / $20.00` and `Combined observed/attributed`. Carried
  unchanged from `paths-digest.md`: the binding constraint is subscription quota,
  not currency. `src/evallab/quota.py` (#64) exists and this digest does not read
  it. That is the QuotaGate/QuotaAccounting line of work, and correcting it here
  would have collided with their lease.
