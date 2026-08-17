Status: review-wanted
Last: PR opened — quota headroom shown at every billable decision; provider-reported exhaustion refuses; #65's 18 tests unchanged and passing
Next: Peter decides whether to set a percentage threshold below the provider's own limit, and whether the promotion sidecar in "What Peter must decide" is wanted
Blockers: none

# GATE: show quota headroom at the moment a paid run is authorised

Branch `role/quota-gate`, based on `origin/main` = `0960eea`. **Correction to the
brief:** it said `origin/main` is `7456ac8`. That is one commit stale — `7456ac8`
is #66, and `origin/main` had already advanced to `0960eea` ("Build plan
(living)") by the time I fetched. I branched from `origin/main`, not from the
named SHA.

## What changed

`src/evallab/quota.py` measures; `PolicyGate` now reads that measurement and
shows it to whoever is authorising. `quota.py` is untouched and still imports
nothing from `queue.py` or `cli.py`, so measurement continues not to authorise.

| Path | Change |
|---|---|
| `src/evallab/queue.py` | quota helpers, `PolicyGate(headroom=…)`, one new refusal, `PaidRunAuthorization.quota_override`, `DirectoryQueue.approve(quota_override=…)` |
| `src/evallab/cli.py` | `approve --despite-quota`; `approve` prints the headroom and warns when dispatch will refuse |
| `policy/standing-approvals.yaml` | one `escalate_to_human` entry + comments |
| `tests/test_quota_gate.py` | new, 22 tests |
| `docs/operations.md` | new section "What the quota gate does and does not decide" |
| `docs/quota-accounting.md` | status line and §"Intended integration" → "Integration, as performed" |

Every billable decision — the `paid_run_unauthorized` refusal at `submit`, the
`approve` output, and the admission at `tick` — carries `used_percent`,
`remaining_percent`, `resets_at`, `hard_stop` with its lockout note, the age of
the reading, and the rollout it came from, each with a `[observed]` /
`[unavailable]` label and an explicit "scope: account, NOT the lab" line.

**Proven live** against real recorded data, no paid agent executed. A scratch
workspace under `derived/` (since removed) with `runs` symlinked to the primary
checkout's read-only `runs/`:

```
$ uv run evallab submit …          # state: waiting, paid_run_unauthorized
subscription quota (scope: account, NOT the lab; provider-reported):
  used_percent         92.0 [observed]
  remaining_percent    8.0 [observed] (account-wide, whole percentage points)
  resets_at            2026-08-20T18:32:49+00:00
  hard_stop            True
    no overflow credits: reaching 100% blocks every paid agent until the window
    resets, it does not incur an extra charge
  observed_at          2026-08-16T14:00:31.683000+00:00
  staleness            11h04m old
  source               event-summary__qDnf3Zr/agent/sessions/2026/08/16/rollout-…jsonl
```

The same block appears under `evallab approve`. With a synthetic rollout
reporting `rate_limit_reached_type: primary`, `approve` added
`WARNING: dispatch will refuse this spec — …` and a real `Executor.tick()`
(fake runner injected, so nothing could reach Harbor) dispatched 0 and wrote
`subscription_quota_exhausted` to `queue/reasons/`. Re-approving with
`--despite-quota` recorded `reason_code: quota_override` on the
`human_approved` event and dispatched 1.

## The stale-reading decision, and why

**A stale reading warns. It never refuses. The age is printed next to every
figure, always, in the same block.**

Argued from the two failure modes rather than chosen:

- **Refusing on age deadlocks the lab, permanently.** Freshness is produced *by*
  paid runs — the reading exists only because a paid trial wrote a `rate_limits`
  block into its rollout. After any quiet period every reading is stale, so
  "refuse when stale" makes the first paid run impossible with no timer that
  ever expires it. That is a strictly worse failure than the one it prevents.
- **Trusting a stale reading silently is how a lockout arrives unannounced —
  and this repository has measured how badly.** `docs/quota-accounting.md`
  records the account moving 71% → 91% in roughly five hours of ordinary
  interactive use, none of it the lab's. A five-hour-old figure has a
  *demonstrated* capacity to be twenty points wrong. Today's live reading was
  5h18m stale when the brief was written and 11h04m by the time I ran it.

The resolution is that the gate is the wrong actor to decide. It cannot make a
stale reading fresh, and a refusal would not have prevented that drift — the
drift was not the lab's. What it *can* do is refuse to present an old number as
a current one. The judgement stays with the named human #65 already requires,
who can look at the provider directly, which this gate deliberately cannot.

One consequence needed its own handling, and finding it changed the design.
**A reading that says *exhausted* can itself be stale**, and a paid trial that
recorded 100% just before the window rolled over would otherwise lock the lab
out forever — same deadlock, arriving through the refusal I was asked to add.
Two things prevent it:

1. `quota_window_expired` — a reading whose `resets_at` has already passed
   describes a window that no longer exists, so it refuses nothing and says so
   in the notice. This is derived from the provider's own `resets_at`, not from
   a rule I invented. The reader's clock is reconstructed as
   `observed_at + staleness_seconds` so the gate reads no clock of its own and
   its tests stay deterministic per `agents/CHECKS.md`.
2. `uv run evallab approve <id> --actor peter --despite-quota` — the deliberate
   override. Recorded as `reason_code: quota_override` on the `human_approved`
   event, **not** on the spec file, for exactly #65's reason: the automation
   writes the spec file. It overrides `subscription_quota_exhausted` and
   `subscription_quota_ceiling` and nothing else; `test_the_override_lifts_
   nothing_except_the_quota_refusal` pins that against the per-job ceiling,
   `paid_run_unauthorized`, and `paid_run_authorization_stale`.

That override is also the answer to "the operator must be able to override
deliberately". **Be clear about what it is not:** because staleness itself never
refuses, there is nothing for an operator to override on a merely-stale reading
— they see the age and proceed, which is the same keystroke as authorising. The
override exists for the one case where quota actually blocks. If you want a
separate, explicit acknowledgement of staleness on every paid approve, that is a
one-line addition to the `approve` handler, but it adds friction to every paid
run to defend against a state the operator was already shown.

## Where a Sponsor-set threshold goes

`REFUSE_BILLABLE_AT_USED_PERCENT` in `src/evallab/queue.py` (immediately below
`QUOTA_STALENESS_NOTE`). It is `None` and stays `None`. Set it to a float and
billable dispatch refuses at or above it, under the **separate** reason code
`subscription_quota_ceiling`, so `queue/reasons/` never records a lab policy as
the provider's statement. `test_a_configured_threshold_refuses_under_its_own_
reason_code` monkeypatches it to 90.0 and proves the mechanism works end to end;
`test_no_threshold_is_invented_below_provider_exhaustion` asserts the committed
value is `None` and that 80/92/99 all dispatch.

Its **durable** home is `policy/standing-approvals.yaml`, and it is not there
because it cannot be: `StandingApprovalsPolicy` is `extra="forbid"`, so adding
`refuse_billable_at_used_percent:` to the YAML without first adding the field
makes `load_policy` raise for every command. `src/evallab/schemas.py` is leased
to `JobsDirContract` this round. **This is the one place my mission was
path-blocked.** The constant is the honest interim home, not the intended one.

## #65 is intact

- All four fail-closed states still fire: `paid_run_unauthorized`,
  `authorization_ledger_unreadable`, `paid_run_authorization_stale`,
  `paid_run_authorization_mismatch`. The quota check sits *after* all
  authorisation-provenance checks and *before* the dollar ceilings.
- **`tests/test_paid_authorization.py`: 18 passed, not one line changed.**
  Baseline before my first edit: 18 passed. After: 18 passed.
- The M4 property survives: `standing_rule_admits` is untouched and still
  returns `False` for every billable spec, so putting a billable agent back into
  `policy/standing-approvals.yaml` grants nothing.
- The nightly canary path is unchanged even at 100%: at `submit` there is never
  an authorisation, so `paid_run_unauthorized` fires before the quota check and
  `canary.py`'s `staged_for_authorization` branch still matches.

## Before / after on the new tests

`tests/test_quota_gate.py` cannot run against `origin/main` at all — it fails at
import, because none of the mechanism exists there. Confirmed absent from
`origin/main:src/evallab/queue.py`: `lab_threshold_reached`,
`provider_reported_exhaustion`, `quota_window_expired`,
`render_headroom_notice`, `REFUSE_BILLABLE_AT_USED_PERCENT`, `quota_override`.
"All 22 fail before, all 22 pass after" is true but says little, so I ran eight
targeted mutations against the *patched* code instead. Each reproduces a defect
this mission was asked to prevent; each is caught:

| Mutation | Caught by |
|---|---|
| M1 notice prints numbers regardless of `availability` (trap 1) | 3 tests, incl. the poisoned-reading test |
| M2 `provider_reported_exhaustion` always `None` | 3 tests |
| M3 an invented 80% threshold | `test_no_threshold_is_invented_below_provider_exhaustion` + 1 |
| M4 override read as always-true instead of from the event | 4 tests |
| M5 expired window still refuses (the deadlock) | `test_an_expired_window_cannot_refuse_and_says_why` |
| M6 staleness hidden from the operator | 2 tests |
| M7 free-control admissions annotated with quota text | 2 tests |
| M8 quota refusal applied to free controls | 2 tests |

M7 initially **escaped**, which is worth recording: my first free-control test
only covered the standing-rule admission path, and a human-approved `oracle`
spec takes a different branch. I widened the test to cover both paths and
re-ran; both mutations are now caught. The gap was real and I would have shipped
it.

## Policy change: tightening only

`policy/` gained exactly two things, neither of which grants anything:

- One entry appended to `escalate_to_human`: `subscription_quota_exhausted`.
  That list is **declarative** — no code reads it (`grep -rn escalate_to_human
  --include=*.py src/` returns only the schema field). Adding an item to a list
  of things requiring a human cannot loosen.
- Comments recording that the dollar figures are list-price equivalents, that
  the binding constraint is a rate-limit window, and where a threshold would go.

No ceiling widened, no agent added to `auto_run`, no `requires` removed, nothing
newly permitted. The one thing that could be *read* as loosening is the new
`--despite-quota` flag, so state it plainly: it overrides a refusal that did not
exist on `origin/main`. Net effect against today's behaviour is strictly more
refusal, never less. It cannot reach any pre-existing control.

## What Peter must decide before relying on this

1. **The threshold, or the deliberate absence of one.** Today the gate refuses
   only at the provider's own limit — at 92% with `hard_stop` true and a
   1-percentage-point counter, that leaves roughly eight points of margin and no
   warning shorter than "you are locked out". If a run that gets refused mid-way
   is worse than a run that never starts, a number belongs in
   `REFUSE_BILLABLE_AT_USED_PERCENT`. I will not pick it.
2. **Whether stale-warns is the right call for *you*.** My argument is above and
   I believe it, but it rests on the claim that a human is reading the output.
   If paid approvals ever become semi-attended, the argument weakens and the
   staleness acknowledgement described above becomes worth its friction.
3. **The promotion gap, still open.** `docs/quota-accounting.md` notes that
   `research/evidence/runs/` correctly excludes `agent/sessions/`, so promotion
   discards the quota signal permanently. That now has a second cost: **this
   gate goes blind on promoted-only history.** A checkout whose paid evidence
   has all been promoted reads `unavailable` and shows UNKNOWN. A redacted
   `rate_limits` sidecar written at promotion time fixes both; it is outside my
   lease (`scripts/promote_codex_bundle.py` belongs to `PromotionQuota`).
4. **UNVERIFIED, unchanged and load-bearing:** whether cached input draws on the
   allowance at the same rate as uncached. 90.7% of input is cached. Nothing in
   this PR depends on it — the gate reads only the provider's own percentage,
   never a token-derived estimate — but any future threshold expressed in tokens
   would.

## Residual defect inherited, not fixed

From `JobsDirContract` over IRC: `evallab run --jobs-dir` (`cli.py:247`,
`cli.py:643-666`) builds a `RunRequest` directly and never an `ExperimentSpec`,
so their new `jobs_dir` validator covers `evallab submit` only. `cli.py` is my
lease this round, but that is not my mission and I did not expand into it.
Recorded here so it does not fall between the two PRs, and recorded in theirs
too. Neither of us should take it this round: `JobsDirContract` argues, and I
agree, that the durable fix is not a second validator in `cli.py` but routing
`evallab run` through the `ExperimentSpec` contract it currently bypasses. That
is its own small mission. Their `results.py` fix does not affect this gate: it
reads only `report.headroom`, which is the single latest observation, and
deliberately counts no trials.

Merge order with their PR #68 is unconstrained — it touches `schemas.py`,
`results.py`, `tests/test_jobs_dir_contract.py`, `docs/architecture.md` and
their handoff; zero overlap with `queue.py`, `cli.py`, or `policy/`.

## Verification

- `uv run pytest` — **691 passed**. `origin/main` baseline measured in a
  throwaway detached worktree: **669 passed**. 691 − 669 = 22, exactly the new
  file, so nothing existing was removed or skipped to get green.
- `uv run ruff check .` — all checks passed.
- `uv run pytest tests/test_paid_authorization.py` — 18 passed, before and after.
- Live: real 92% reading through the real CLI, shown above. **No paid agent was
  executed at any point.** Every dispatch in every check used an injected fake
  runner; the only real subprocess run was `python -m evallab.quota`, which
  reads files.
- Catalog unchanged, verified read-only after all work: `72 jobs / 23
  trajectory_documents` (`docker exec eval-lab-postgres-1 psql -U evallab -d
  evallab -tAc "select count(*) …"`). Nothing in this PR writes to it.
- Not run, per the brief: `scripts/premerge.sh`, project-wide formatters. CI is
  the merge authority (`agents/CHECKS.md`).
