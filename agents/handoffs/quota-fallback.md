Status: review-wanted
Last: `quota._rate_limit_snapshots` now falls back to the promoted R4 quota sidecar when a trial's rollouts yield no snapshot; committed evidence alone reports 67 snapshots / 70.0 `[observed]`
Next: Integrator merges the PR once GitHub is green; a Sponsor decision on promoting the three `-20260816` jobs is what would raise the committed reading from 70.0 to 92.0
Blockers: none

# QUOTA-FALLBACK — read the promoted rate-limit sidecar when no rollout survives

Branch `role/quota-fallback`, worktree `.worktrees/quota-fallback`, branched from
`origin/main` at `02f8d89` (fetched at start; `git rebase origin/main` reported
"up to date" before final validation, so no rebase was needed).

## What changed

| Path | Change |
|---|---|
| `src/evallab/quota.py` | new `_sidecar_snapshots`; `_rate_limit_snapshots` falls back to it; two module-docstring lines and two constants |
| `tests/test_quota.py` | new `add_quota_sidecar` helper, `PROMOTED_RUNS`, 8 tests |
| `docs/quota-accounting.md` | "The reader is not wired yet" rewritten as landed; post-promotion bullet, boundaries list, known-gap and reproduction sections updated |
| `agents/handoffs/quota-fallback.md` | this file |

Nothing outside the lease was touched. `queue.py`, `cli.py`, `digest.py`,
`schemas.py`, `craft.py`, `policy/` and every promoted bundle are untouched —
`git status --porcelain` is clean and the diff is confined to the four paths
above.

## The defect, and the fix

`_rate_limit_snapshots` globbed `agent/sessions/**/rollout-*.jsonl` only.
Promotion omits that path under rule R2 and #67 preserved the reading beside it
as `<trial>/agent/quota/*.rate-limits.json`, so promoted evidence *carried* the
quota signal while the reader could not see it: `snapshots harvested: 0`,
`headroom.availability "unavailable"`.

The fix is the change #67 specified, verified rather than redesigned: when a
trial's rollouts yield no snapshot, read that trial's sidecars, accept only
`kind == "evallab-rate-limits-sidecar"`, and keep
`(_parse_instant(entry["timestamp"]), entry["rate_limits"], sidecar_path)` per
entry in `snapshots`.

Keyed on *snapshots yielded*, not on rollout files existing — as specified. A
truncated rollout that recorded no reading therefore still lets the committed
reading answer, and a rollout with readings always wins.

## Fallback, not addition

A live run has the rollout, a promoted bundle has the sidecar; a tree holding
both holds one history twice. `_rate_limit_snapshots` consults the sidecars only
when the rollouts yielded nothing at all.

`proven live` — the tests are not vacuous. Three mutations of the shipped code,
each run against the new tests:

| Mutation | Result |
|---|---|
| fallback line deleted | 6 failures, incl. `test_committed_evidence_alone_yields_an_observed_headroom` (`assert 0 >= 67`) |
| `if not snapshots:` → `snapshots.extend(_sidecar_snapshots(...))` | `test_a_rollout_and_a_sidecar_on_one_trial_count_the_rollout_once` fails `assert 4 == 2` |
| sidecar reading dated by file mtime instead of the trial instant | `test_a_sidecar_reading_ages_exactly_as_its_rollout_twin` fails (`2026-08-17T01:45` vs `2026-08-15T06:30:02`) |

## Acceptance, reproduced

`proven live`, committed evidence only (`runs/` does not exist in this
worktree), `now` injected as `2026-08-16T18:00:00Z`:

```
REMAINING on the subscription (scope: account, NOT the lab)
  used_percent                         70.0 [observed]
  remaining_percent                    30.0 [observed]
  limit_id / plan_type                 codex / prolite
  window                               10080 minutes (168h00m)
  resets_at                            2026-08-20T18:32:49+00:00
  observed_at                          2026-08-15T07:02:25.846000+00:00
  staleness                            34h57m
  credits_balance                      0
  hard stop                            True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  counter resolution                   1.0 percentage point
  source                               event-summary__5E3btLv/agent/quota/rollout-2026-08-15T07-02-04-01a0043a-4b83-7252-a594-fa289617124f.rate-limits.json
  lab's share of that percentage       [unavailable]

snapshots harvested: 67 [observed]
```

Before the change the same command printed `remaining allowance [unavailable]`
and `snapshots harvested: 0`. 67 matches the sum of `snapshot_count` across the
nine committed sidecars (5+6+6+16+13+9+4+4+4).

## The two load-bearing properties

- **Staleness stays honest.** The instant kept is the one the trial recorded,
  never the file's. A rollout tree and a sidecar tree carrying the same reading
  report identical `observed_at` and identical `staleness_seconds` (127798.0 at
  the injected `now`), and `source` ends `.jsonl` for one and
  `.rate-limits.json` for the other, so a reader can tell which record answered.
- **The account/lab split is untouched.** `Headroom` remains account-scope and
  provider-reported; `lab_attributable` stays permanently `[unavailable]`. No
  model field was added, renamed, or widened.

`_model_turns` needed no change, and that was checked rather than assumed:
`test_a_sidecar_only_trial_reports_no_model_turns_rather_than_zero` asserts a
sidecar-only trial reports `model_turns is None`.

## What a fresh clone will and will not see

It **will** see 67 snapshots and an `[observed]` headroom of `used_percent`
70.0, 34h57m stale, hard-stop true, window resetting
`2026-08-20T18:32:49+00:00`.

It **will not** see 92.0. All three 92.0 readings live in the unpromoted
`canary-*-codex-20260816` jobs, which exist only in one workstation's gitignored
`runs/`. The three promoted bundles top out at 70.0. R4 makes the signal survive
promotion; it cannot promote a bundle. Promoting those jobs is a Sponsor
decision and was not taken here.

## Effect on the billable gate (#70), unchanged contract

`Executor._repo_headroom` scans `default_roots`, so on a fresh clone
`render_headroom_notice` now prints an observed 70.0 with its staleness and
hard-stop instead of UNKNOWN. It refuses nothing:
`provider_reported_exhaustion` triggers at 100.0 and
`REFUSE_BILLABLE_AT_USED_PERCENT` is committed as `None`. Checked live with
`now` injected: at `2026-08-16T18:00Z` `quota_window_expired` is `False`; at
`2026-08-21T00:00Z` (past `resets_at`) it is `True`, so the same committed
reading is correctly treated as a window that no longer exists rather than as a
stale balance. Trap one is respected upstream — `availability` is still checked
before any percentage is read.

## Recorded, not fixed (outside this lease)

1. **`ConsumptionTotals.model_turns` renders `0 [observed]` where every trial's
   turn count is unavailable.** Pre-existing and visible before this change:
   `totals.model_turns += trial.model_turns or 0` sums `None` as zero, so
   promoted evidence prints `model turns 0 [observed]` although
   `TrialConsumption.model_turns is None` for all nine trials. The per-trial
   field is honest; the aggregate is not. Fixing it means an availability-aware
   total, which is a model change #67 did not specify and this mission did not
   take. Whoever owns the totals should decide.
2. **`reasons_for(...)[-1]` is a flaky ordering assumption.** Observed once:
   `tests/test_paid_authorization.py::test_authorization_does_not_lift_the_per_job_cost_ceiling`
   failed in a full-suite run with `'paid_run_unauthorized' == 'per_job_cost_ceiling'`,
   then passed in three isolated runs and in two subsequent full-suite runs.
   Cause, `proven live`: reason files are named `{spec_id}-{new_ulid()}.json` and
   `new_ulid` is a 48-bit millisecond timestamp plus 80 random bits, so two
   reasons written inside the same millisecond sort randomly — 1019 of 2000
   same-millisecond pairs sorted in reverse. `sorted(...)[-1]` is therefore not
   "the newest reason". Eight sites in
   `tests/test_paid_authorization.py` and `tests/test_quota_gate.py`; neither
   file is this mission's lease. It is not caused by this change: the same
   ordering hazard exists on `origin/main`.

## Brief corrections

- The brief said "one function". Shipped as two: `_sidecar_snapshots` holds the
  sidecar read and `_rate_limit_snapshots` holds the one-line fallback, which
  keeps the discipline (`if not snapshots: return _sidecar_snapshots(...)`)
  visible in one line instead of buried in a second loop. Behaviour is exactly
  what was specified.
- `agents/CHECKS.md` requires `make premerge` (locked install, ty ratchet)
  before pushing; the mission brief forbids `scripts/premerge.sh`. The brief was
  followed: `uv run pytest` and `uv run ruff check .` only. GitHub remains the
  merge authority for the locked-install and ty gates.
- `agents/missions/ACTIVE.md` was read and, as the brief states, is stale; it
  was not relied on and not edited.

## Verification

```
uv run pytest              # 763 passed, 1 xfailed (an earlier full-suite run hit
                           # the pre-existing ULID-ordering flake described above)
uv run ruff check .        # All checks passed!
uv run python -m evallab.quota   # 67 snapshots, 70.0 [observed], from committed evidence
```

Nothing billable ran: no `codex`, no `claude-code`, no Harbor dispatch, no
container. Postgres and Phoenix were not touched — the shared catalog was never
queried or written by this mission.
