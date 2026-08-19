# Campaign card — gym-v0 baseline, wave 1

**Status: PRE-REGISTERED, ZERO TRIALS RUN.** This card exists so the campaign's
design is on the record before any data arrives, and so the reason no data
arrived is auditable. It reports **no rates, no intervals, and no findings**,
because none exist. Nothing in this file may be cited as a result.

## Question

Does the gym's frozen task set (`gym-v0`) produce a stable baseline pass rate for
`codex` at k=3, with a free `oracle` control per task family confirming the
instruments still read true?

Purpose: `baseline`. All results, when they exist, cite the frozen manifest
`library/frozen/gym-v0/manifest.json` so that next month's numbers are comparable
to tomorrow's.

## Configuration and evidence

| Field | Value |
|---|---|
| Frozen set | `gym-v0` |
| Registered tasks in `gym-v0` | **0** |
| Planned arms | every gym-v0 task × `codex` × k=3, purpose=baseline; plus one `oracle` control per task family (free) |
| Trials dispatched | **0** |
| Trials scored | **0** |
| Evidence rows | none |

Wave 1 was sized by `evallab preflight` and came out at **zero codex trials**.
Two independent blockers, both measured at 2026-08-19T07:28Z rather than assumed:

**1. The registry is empty, so there is nothing to run.**

```
$ uv run python -m evallab.cli registry list
No task records found in library/registry/.
```

`library/registry/` contains only `.gitkeep`. `registry.py:370` refuses any spec
whose task is not registered, so no experiment can be submitted at all — the
campaign has zero tasks and therefore zero task families, which also removes the
free `oracle` control arm. Registry promotion is human-only by the standing
never-list, so this is not something the campaign can resolve.

**2. codex has no headroom, and the reading is stale.**

```
codex
  used_percent             92.0 [observed]
  remaining_percent        8.0  [observed] (account-wide)
  window                   10080 minutes (168h00m)
  resets_at                2026-08-20T18:32:49+00:00
  observed_at              2026-08-16T14:00:31Z
  staleness                65h27m old
  credits_balance          0
  hard stop                True
    no overflow credits: reaching 100% blocks every paid agent until the window
    resets, it does not incur an extra charge
```

Preflight's own words: *"UNKNOWN is not 'plenty left'. This says the allowance
could not be measured, not that a run fits inside it."* 8% of a weekly window
with zero overflow credits does not hold a full k=3 sweep, and the freshest
available reading is 65 hours old. The campaign brief's instruction is to run
quota-sized waves across nights; tonight's quota-sized wave is empty.

## Result

**No result.** Zero trials ran, so there is no rate to report and no interval to
compute. The correct reading of this card is "the campaign is defined and
blocked", not "the campaign found nothing".

When trials exist, rates will be reported with n and a Wilson interval via
`evallab cohort`, per the uncertainty contract (T4) — never as a bare percentage.

## Elicitation

Elicitation caveat: the planned configuration is the **default harness** with no
preamble, `k=3` attempts, agent `codex`, model as pinned by the harness at run
time. That tuple is part of the measurement, not a neutral background: the same
model under a different harness or preamble is a different number, which is the
whole premise of the scaffold-effect experiment class (EXP-S03). The
`extra_instruction_path` field landing in this same mission is the lever that
will let that arm vary the preamble deliberately.

Elicitation parameters to be recorded per trial when the campaign runs: agent
version, model pin, preamble hash, toolset, attempts k.

## Contamination

Contamination caveat: **status unknown per task and must be recorded per task
before any capability claim.** `gym-v0` is empty, so there is not yet a single
task whose exposure has been assessed. Any task promoted from a public benchmark
carries pretraining-exposure risk, and any task derived from this repository's own
corpora carries leakage risk from its own solution files. Until each task's
exposure is stated, results from this campaign support **behaviour** comparisons
only, never capability claims about a model.

External corpora being acquired in parallel (Harbor-Index, TB 2.1 trajectories)
are public models' rollouts on public tasks and are **behaviour-study material
only** — they must never be mixed into this campaign's numbers.

## Threats to validity

1. **Zero-task baseline is not a baseline.** If tasks are registered later and a
   wave runs, the frozen manifest will differ from this card's `gym-v0` (which is
   empty). The manifest is frozen by contract, so a non-empty set is `gym-v1` and
   this card does not describe it. Do not retro-fit.
2. **Stale quota reading.** The 8%-remaining figure is 65h old; the true value
   could be lower (more spend since) but not meaningfully higher inside one
   window. Sizing on a stale reading is a known weakness of the current preflight
   path, not a claim about spend.
3. **k=3 is a floor, not a choice.** k=3 is the canary policy cap; the detectable
   effect at k=3 on a small task set is coarse. Peter decision #1 (raise the
   per-job ceiling to allow k=5) is what would sharpen it — the binding constraint
   is the `$3` `per_job_cost_ceiling_usd` in `policy/standing-approvals.yaml:30`,
   not the estimated compute cost.
4. **Instrument re-validation is absent.** The free `oracle` control per family
   is the check that the harness and verifier still read true. With zero families
   there is no such check, so even a later partial wave should re-add it before
   any number is read.

## Regeneration query / command

```bash
# What was measured to size wave 1 (both re-runnable today):
uv run python -m evallab.cli registry list
uv run python -m evallab.cli preflight

# The frozen set this card cites:
cat library/frozen/gym-v0/manifest.json

# When tasks exist, the campaign submits through the CLI only (never hand-written
# queue files) and every spec carries purpose=baseline:
#   uv run python -m evallab.cli submit <spec.yaml>
#   uv run python -m evallab.cli approve <spec_id> --actor <human>
```

## Human review

Not reviewed. Written by the integrator session on 2026-08-19 as the honest
cycle-3 record for GYM-RUN. It needs no scientific review because it contains no
findings; it needs a **decision**, which is Peter's:

- **Register the curated-nominee slice as tasks, or reject the study** (Peter
  decision #2 in `docs/prompts/gym-campaign.md`). Until then `gym-v0` is empty and
  wave 1 cannot be sized above zero regardless of quota.
- Separately, **raise the per-job ceiling for k=5 or accept k=3** (decision #1).

Both were already on the doc's escalation list; this card records that decision #2
is now the binding constraint on the entire campaign, not a side question.
