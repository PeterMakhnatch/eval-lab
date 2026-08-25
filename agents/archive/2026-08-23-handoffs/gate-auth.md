Status: done
Last: merged as PR #65 (`1e0a8b3`)
Next: none
Blockers: none

# GATE-AUTH — require explicit authorisation before any paid agent runs

Worktree `.worktrees/gate-auth`, branch `role/gate-auth`, rebased onto
`origin/main` at `827bd1c`.

## The defect, restated

`policy/standing-approvals.yaml` listed `canary` under `auto_run` with
`agents: [codex, claude-code]`. `policy/canary-suite.yaml` declares three
members and `attempts: 3`, so the nightly cycle enqueued and immediately
dispatched three paid Codex jobs of three attempts each — nine paid sessions a
night, unattended, on 2026-08-14, -15 and -16. Every dollar ceiling reported
green because `PolicyGate` compared `spec.est_cost_usd` against dollars while
`src/evallab/runner.py` authenticates Codex from `~/.codex/auth.json`, i.e. the
Sponsor's ChatGPT subscription. No dollar figure in this repository measured
what was consumed.

## Mechanism chosen

**A billable spec is admitted only against a `PaidRunAuthorization` read from
the append-only queue event log, and standing approvals are never consulted for
billable work at all.** Two code-level facts, in `src/evallab/queue.py`:

1. `standing_rule_admits(rule, spec)` returns `False` whenever `spec.billable`.
   Every `auto_run` match now goes through this function, so **no edit to
   `policy/standing-approvals.yaml` can re-open unattended spend.** Listing
   `codex` under `auto_run` grants nothing.
2. `PolicyGate.decide` refuses a billable spec with `paid_run_unauthorized`
   before ceilings, before the environment check, before the rule loop. The
   `human_approved: bool` parameter is gone; `authorization: PaidRunAuthorization
   | None` replaces it.

The authorisation record is the `human_approved` event that
`DirectoryQueue.approve` already wrote to `queue/events.jsonl` — append-only,
`flock`-guarded, rotated, retained. `DirectoryQueue.authorizations()` folds the
log into `{spec_id: PaidRunAuthorization(spec_id, actor, authorized_at)}`;
`human_rejected` withdraws a grant.

**The spec file is no longer trusted.** The old executor read
`spec.policy_rule == "human-approval"` straight off the queued artifact — a
field in a file the submitting automation itself writes. That path is deleted.

Fail-closed cases, all proven by test:

| Condition | Reason code | Effect |
|---|---|---|
| billable, no authorisation | `paid_run_unauthorized` | parked in `waiting/` |
| `queue/events.jsonl` unreadable | `authorization_ledger_unreadable` | **whole tick** dispatches nothing, free controls included |
| authorisation predates the spec's `submitted_at` | `paid_run_authorization_stale` | refused — a spec id is not a reusable token |
| authorisation names another spec | `paid_run_authorization_mismatch` | refused |
| billable spec with no `submitted_at` | `paid_run_authorization_mismatch` | refused (cannot prove coverage) |

`Executor._tick_locked` reads the ledger once per tick; `_retry_within_policy`
re-reads it per retry, so withdrawing an authorisation mid-flight stops the next
transient retry.

### Why this and not the alternatives

- **Rejected: removing billable classes from `auto_run` only.** This is the
  option the brief offered and it is the *weakest*: it is one line of YAML away
  from being undone, and `AGENTS.md` already said agents must never loosen
  policy — which did not stop the lab from running nine paid sessions a night.
  A file that documents an intention is not a mechanism. I did make this change
  as well, but as a consequence, not as the control.
- **Rejected: a new authorisation token/record file** (`queue/authorizations/`)
  plus a new `evallab authorize` command. It duplicates `approve`, adds a second
  operator verb for one decision, and adds a file format whose contract model
  would have to live outside `src/evallab/schemas.py` (not in my lease). The
  event log already *is* the durable, locked, audited record of a human
  decision; a second one would only create the question of which is
  authoritative.
- **Rejected: a new `approved_by_human` field on `ExperimentSpec`.** It puts the
  proof back in the file the automation writes — the exact defect — and
  `schemas.py` is outside my lease.
- **Rejected: refusing to load a policy that lists a billable agent.** Maximal,
  but it would make M006's fenced analysis-worker tests and several registry
  tests fail on a *configuration* they legitimately construct, and it converts a
  misstatement into a crash. The gate ignoring such a rule is enough.

## Which classes require authorisation

| Class | Agents | Unattended? |
|---|---|---|
| `local-controls` | `oracle`, `nop` | **yes**, unchanged, no ceremony |
| everything else | `codex`, `claude-code`, any future paid adapter | **no**, one recorded authorisation per spec |

`billable` is `agent not in {"oracle", "nop"}` (`schemas.py:57`), so a new paid
adapter is refused by default without any policy edit.

## The `policy/` diff, in full — nothing widened

```diff
+# Standing approvals: what may run WITHOUT a human in the loop.
+# (9 comment lines explaining that auto_run cannot grant paid execution)
 version: 1
 daily_cost_ceiling_usd: 20
 per_job_cost_ceiling_usd: 3
 quiet_failure_rule: 3
 auto_run:
   - name: local-controls
     agents: [oracle, nop]
-  - name: canary
-    tasks: [canary/*]
-    agents: [codex, claude-code]
-    max_attempts: 3
-  - name: researcher-followups
-    tasks: [registered/*]
-    agents: [codex, claude-code]
-    max_attempts: 5
-    requires: [schema_valid, dedup_pass, calibrated_judges_only]
 escalate_to_human:
+  - any_billable_agent
   - new_task_registration
   - cloud_or_remote_environment
   - anything_exceeding_ceilings
```

Explicitly:

- **No ceiling widened.** `daily_cost_ceiling_usd: 20`,
  `per_job_cost_ceiling_usd: 3`, `quiet_failure_rule: 3` are byte-identical.
  Authorisation does not lift any of them — `test_authorization_does_not_lift_the_per_job_cost_ceiling`.
- **No agent added to any `auto_run` entry.** `local-controls` is unchanged;
  the other two entries were deleted, not edited.
- **No `requires` gate removed from a rule that still grants anything.** The
  `requires: [schema_valid, dedup_pass, calibrated_judges_only]` line disappears
  only because its whole rule did. That rule granted nothing today
  (`calibrated_judges_only` is false) and grants nothing under the new gate,
  since `researcher-followups` names only billable agents.
- **One line added to `escalate_to_human`.** `any_billable_agent` is a
  description of a refusal, not a permission; that list is not consulted for
  admission.

**Consequence for M006, recorded not fixed:** `analysis_worker.py:601` looks for
an `auto_run` rule named `researcher-followups`. It now finds none and defers
with `policy_rule_absent:researcher-followups` instead of
`policy_agent_not_listed:_no_adapter`. Both are closed; M006's gate was already
CLOSED with `_no_adapter` as its default, so nothing that ran before stops
running. M010 will need to reintroduce that rule — and re-introducing it is a
*loosening*, i.e. the Sponsor's call, which is the right place for it. Note that
even with the rule restored the new gate refuses billable dispatch without an
authorisation, so M010's "queue-authorized bounded adapter" must go through
`approve`.

## Tests: before/after, per test

New file `tests/test_paid_authorization.py`, 18 tests. They build their own
policy — `permissive_policy()`, a verbatim copy of the pre-fix
`standing-approvals.yaml` including `codex` and `claude-code` under `canary`
and `researcher-followups` — so they prove the gate holds *against* the loosest
policy this repository ever carried, not merely against the tightened file.

Run against the unpatched `origin/main` sources the module does not import
(`standing_rule_admits`, `PaidRunAuthorization` do not exist), so "before" is
established by mutation instead: each mutation is the *smallest future edit*
that reintroduces the defect, applied to the merged code, with the tests
unchanged.

**M1 — the exact pre-fix behaviour.** Delete `if spec.billable: return False`
from `standing_rule_admits` and delete the `authorization is None` refusal, so
billable specs fall through to `auto_run` again. **12 of 18 fail:**

| Test | before (M1) | after |
|---|---|---|
| `test_billable_spec_without_authorization_is_parked_not_approved` | FAIL `assert not True` | pass |
| `test_refusal_names_the_agent_and_the_exact_next_command` | FAIL `'codex' in 'admitted by standing policy rule canary'` | pass |
| `test_unattended_billable_spec_never_reaches_harbor` (`test_unauthorized_billable_spec_never_reaches_harbor`) | FAIL `assert 1 == 0` — it dispatched | pass |
| `test_recorded_authorization_admits_and_dispatches_the_same_spec` | FAIL | pass |
| `test_rejecting_an_authorized_spec_withdraws_the_authorization` | FAIL | pass |
| `test_nightly_cycle_enqueues_paid_canaries_but_dispatches_none` | FAIL `assert 3 == 0` — three paid dispatches | pass |
| `test_an_authorization_cannot_be_replayed_by_a_later_spec_reusing_its_id` | FAIL | pass |
| `test_an_authorization_for_one_spec_does_not_cover_another` | FAIL | pass |
| `test_no_standing_rule_can_admit_a_billable_agent` | FAIL `assert not True` | pass |
| `test_the_gate_refuses_a_billable_spec_that_carries_no_submission_time` | FAIL | pass |
| `test_tick_dispatches_nothing_when_the_authorization_ledger_is_unreadable` | FAIL | pass |
| `test_transient_retry_stops_once_the_authorization_is_withdrawn` | FAIL | pass |

The six that survive M1 are the ones M1 does not touch: both free-control
tests, the free-control-during-corruption test, the per-job ceiling test, the
committed-policy assertion, and `test_a_spec_file_cannot_authorize_itself`.

**M2 — trust the spec file again.** In `_tick_locked`, fall back to
`PaidRunAuthorization(...)` when `spec.policy_rule == "human-approval"`.
**1 of 18 fails:** `test_a_spec_file_cannot_authorize_itself` — FAIL
`assert 1 == 0`, i.e. a hand-written `approved/` spec claiming
`"policy_rule": "human-approval"` dispatched Codex. Passes on the merged code.
This is the test that guards the specific trust boundary the old executor got
wrong.

**M3 — fail open.** Replace the `authorization_ledger_unreadable` early return
with `authorizations = {}`. **2 of 18 fail:**
`test_tick_dispatches_nothing_when_the_authorization_ledger_is_unreadable` and
`test_free_controls_are_also_held_when_the_ledger_is_unreadable`, both with an
uncaught `ValueError` escaping `tick()` rather than a clean refusal — the fail-
open version does not even degrade gracefully.

**M4 — re-widen the policy file.** Put `canary` with `[codex, claude-code]` back
into `policy/standing-approvals.yaml`. **1 test fails**, and only one:
`test_committed_policy_lists_no_billable_agent_under_auto_run`. The other 17,
and the whole 607-test suite, stay green — which is the point: the file is now
documentation, and the gate is the mechanism.

Full-suite deltas: `origin/main` at `fa11f18` was 565 passed; the branch is
**607 passed** after rebase onto `827bd1c` (which itself added tests).
`uv run ruff check .` clean.

## Live proof (proven live, in this worktree's own gitignored `queue/`)

```
$ uv run evallab submit queue/smoke/paid.json
spec_id: 01M05ZVQPF2WK0KJFXWQ9BJHMR
state: waiting
codex is a billable agent. Paid execution here draws on Peter's ChatGPT subscription, so it never runs unattended: this spec waits until a named human authorises it.
  authorise: uv run evallab approve 01M05ZVQPF2WK0KJFXWQ9BJHMR --actor <you>
  refuse:    uv run evallab reject 01M05ZVQPF2WK0KJFXWQ9BJHMR --actor <you> --reason "<why>"
  then run:  uv run evallab tick
The free oracle and nop controls are unaffected and still run unattended.

$ uv run evallab submit queue/smoke/free.json
state: approved
admitted by standing policy rule local-controls

$ uv run evallab approve 01M05ZVQPF2WK0KJFXWQ9BJHMR
evallab approve: error: the following arguments are required: --actor

$ uv run evallab approve 01M05ZVQPF2WK0KJFXWQ9BJHMR --actor gate-auth-smoke
authorized: 01M05ZVQPF2WK0KJFXWQ9BJHMR
actor: gate-auth-smoke
spend: codex x 3 attempt(s), estimated 2.50 USD per job, billed to Peter's ChatGPT subscription
next: uv run evallab tick
```

Both smoke specs were then rejected; `authorizations()` is `{}` and
`queue/approved/` and `queue/running/` are empty. **No paid agent was executed
by this mission.** `tick` was never run on a billable spec. Shared catalog
verified unchanged before and after: `72|23` jobs / `trajectory_documents`.

## Lease spill — five files outside my stated lease, all forced

My lease was `src/evallab/queue.py`, `src/evallab/cli.py`, `policy/`,
`tests/test_queue.py`, a new test file, `docs/operations.md`,
`docs/execution-tiers.md`, this handoff. I also changed:

1. **`src/evallab/calibrate.py`** (8 lines). `dispatch_approved_codex_calibration`
   called `gate.decide(...)` and required `policy_rule == "researcher-followups"`.
   Removing `human_approved` from the signature forced the call site, and the
   rule it demanded no longer exists. It now passes
   `executor.queue.authorization_for(spec)` and requires `"human-approval"` —
   strictly tighter: that paid calibration now needs a recorded authorisation.
2. **`src/evallab/canary.py`** (12 lines). `enqueue()` raised `RuntimeError` on
   any non-admitted decision, which made the nightly cycle quarantine every
   night once the gate started refusing canaries. It now treats
   `paid_run_unauthorized` + `waiting/` as successful *staging* and still raises
   for every other refusal. Without this the fix would have converted a spend
   defect into a permanent false alarm.
3. **`tests/test_canary.py`**. `test_canaries_run_two_consecutive_nights_with_three_attempts`
   asserted six unattended Codex dispatches — it asserted the defect. Rewritten
   as `test_canaries_are_staged_two_consecutive_nights_and_never_self_dispatch`,
   plus a new `test_authorizing_one_staged_canary_dispatches_exactly_that_one`.
4. **`tests/test_registry.py`, `tests/test_unattended.py`**. Both constructed
   codex specs and expected standing-rule admission; both now go through
   `approve`. `test_human_approval_cannot_bypass_unregistered_or_candidate`
   keeps its meaning — registry refusals still beat an authorisation.
5. **`scripts/profile/harness.py`** (1 line). Its synthetic oracle specs carried
   `policy_rule: "human-approval"` to get themselves into `approved/` — the
   forgeable path this mission closed. Changed to `"local-controls"`, which is
   what they actually are. Four `test_profile_harness` tests were failing on it.

None of these are held by another mission in this batch; `QuotaAccounting`
confirmed over IRC that its lease is a new `src/evallab/quota.py` and does not
touch `queue.py`, `cli.py`, or `policy/`.

## Needs Peter — three things before this can be relied on

1. **`approve` is not authentication.** Anything with shell access to this
   machine can run `uv run evallab approve <id> --actor peter`. I removed the
   `--actor peter` *default* so no command silently attributes an approval to
   you, and the actor is recorded, but the name is self-asserted. Making it real
   needs something outside this codebase — a Keychain-held secret the command
   must present, or a signature over the spec digest. **Until then the control
   is "no unattended process approves anything", not "only Peter approves".**
   That is a real and sufficient fix for the defect that occurred (an unattended
   scheduler), and it is not a fix for a compromised or careless agent session.
2. **The ceilings are still denominated in dollars, and dollars are the wrong
   unit.** I did not touch them — re-denominating them is yours. Per
   `QuotaAccounting` (PR #64, green on head `1f77a3d`; supersedes an earlier
   report of mine that said the signal was unavailable): Harbor *does* copy the
   container session rollout to `<trial>/agent/sessions/.../rollout-*.jsonl`,
   and the provider's `rate_limits` block is in it — 137 snapshots across 17 of
   the 33 Codex trials. So the real constraint is observable, and it is worse
   than a budget: `credits.balance` is `"0"` with `has_credits` and `unlimited`
   both false, so `hard_stop` is true. **Reaching 100% is not an overage
   charge; it is a lockout for every paid agent until the window resets** — a
   10080-minute window, latest reading 92.0% used, resetting 2026-08-20. That
   is the argument for this precondition, and it is stronger than any dollar
   figure.
   The account-wide percentage cannot be decomposed into the lab's share, so a
   percentage ceiling is not implementable here; the honest replacement for
   `est_cost_usd` ceilings is a **paid-trial / paid-attempt count** ceiling.
   That is a policy value only you can set.
3. **Nightly still stages nine paid canary specs per night into `waiting/`.**
   With both LaunchAgents unloaded this is inert. If you re-enable them, decide
   whether you want a nightly queue of paid work awaiting your decision (useful:
   it tells you the canary is due) or whether the canary suite should shrink or
   drop to `oracle`. `policy/canary-suite.yaml` declares `agents: [codex]`; I
   did not change it, because reducing the canary's coverage is a research
   decision, not a spend control.

## Correction to the brief

The brief offered "removing billable classes from `auto_run` entirely" as a
candidate for "the smallest change that makes unattended paid execution
structurally impossible". It is the smallest change, but it is not structural:
the thing that produced the defect was a policy file that permitted something,
and the repair cannot be another policy file that permits less. I did both, and
the YAML is now the weaker half of the fix.

Separately, `docs/operating-manual.md:75` still tells the reader an experiment
can be admitted "by editing policy so the class is standing." That is now false
for billable work. It is outside my lease; **integrator, please route that one
line to whoever owns that file.**

**Magnitude, corrected downward — the fix stands anyway.** The brief frames the
nine nightly Codex sessions as the thing consuming the subscription.
`QuotaAccounting`'s timeline (PR #64 body) shows the account-wide counter read
70% immediately before and throughout all nine 2026-08-15 trials and 71% at the
next reading 9h26m later, then 71→91 that evening during interactive use.
Counter resolution is one percentage point, so the honest statement is that the
nightly's contribution **was not detectable** and cannot exceed 1 of those 22
points. This does not weaken the case: the exposure was an unattended loop with
no human in it and no ceiling that measured the binding resource, against an
account whose overrun is a four-day lockout rather than a bill. A reviewer
should not conclude from "the 92% was not the lab's" that the gate is
unnecessary.

**CI note for the integrator.** `agents/CHECKS.md:17` states the ty ratchet is
33; `.github/workflows/typecheck.yml:24` enforces `TY_BASELINE: 28`. The
workflow is what fails a PR. This branch measures **28 diagnostics** —
`uvx ty@0.0.71 check src/ --output-format=concise`, at baseline, no regression
from this change. `agents/CHECKS.md` is outside my lease; the stale number is
recorded here rather than fixed.

## Known-unfixed, deliberately

- **F-01** (no command stages an analysis request) is untouched, per the batch
  constraint.
- I did not add an `evallab authorize` verb, an authorisation expiry, or a
  per-day authorisation budget. Each is defensible; none is needed to close
  this defect, and each would have widened the surface a reviewer must check.
