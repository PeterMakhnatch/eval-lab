# M040 — Quota accounting for cursor-cli and antigravity-cli lanes

Status: complete — ready for review
Last: extended quota accounting in `src/evallab/quota.py` (`PAID_AGENTS`) to include `cursor-cli` and `antigravity-cli`, updated contract tests in `tests/test_quota.py` and `tests/test_preflight.py`, and verified preflight and premerge.
Next: nothing in this slice; standing approvals / policy updates for new lanes remain human-owned.
Blockers: none.

## Problem

`evallab preflight` previously reported headroom for only two agent lanes (`claude-code` and `codex`). `cursor-cli` and `antigravity-cli` were live and registered in `credentials.py` and `profiles.py`, but omitted from `PAID_AGENTS` in `quota.py`. Consequently, `preflight` omitted them entirely instead of reporting their headroom or truthful unobserved state.

## What landed

| File | Change |
|---|---|
| `src/evallab/quota.py` | Added `"cursor-cli"` and `"antigravity-cli"` to `PAID_AGENTS`. |
| `tests/test_quota.py` | Updated `test_only_paid_agents_enter_the_ledger` and `test_for_agent_narrows_the_ledger` to cover all 4 paid agents; added `test_cursor_and_antigravity_consumption_and_headroom` and `test_cursor_and_antigravity_unobserved_headroom_gives_honest_reason`. |
| `tests/test_preflight.py` | Updated `test_one_provider_reading_is_never_attributed_to_the_other` to assert all four lanes; added `test_preflight_renders_all_four_lanes`. |
| `docs/repo-map.md`, `docs/INDEX.md` | Regenerated documentation index and repository map. |

## Preflight output (`uv run python -m evallab.cli preflight`)

```text
evallab preflight — is it safe and sensible to run right now
generated_at: 2026-08-19T21:04:27.844766+00:00
repository:   /Users/petermakhnatch/Developer/eval-lab/.worktrees/m040-quota
quota roots:  /Users/petermakhnatch/Developer/eval-lab/.worktrees/m040-quota/runs, /Users/petermakhnatch/Developer/eval-lab/.worktrees/m040-quota/research/evidence/runs

PER-PROVIDER REMAINING QUOTA (scope: account, NOT the lab; provider-reported)

antigravity-cli
  remaining allowance      UNKNOWN [unavailable]
    reason: no paid trial in the scanned job directories recorded a provider quota snapshot, so the remaining allowance is unknown
    UNKNOWN is not 'plenty left'. This says the allowance could not be measured, not that a run fits inside it. Check the provider yourself before authorising anything billable.
  paid trials seen         0 [observed]
  quota snapshots          0 [observed]

claude-code
  remaining allowance      UNKNOWN [unavailable]
    reason: no paid trial in the scanned job directories recorded a provider quota snapshot, so the remaining allowance is unknown
    UNKNOWN is not 'plenty left'. This says the allowance could not be measured, not that a run fits inside it. Check the provider yourself before authorising anything billable.
  paid trials seen         0 [observed]
  quota snapshots          0 [observed]

codex
  used_percent             70.0 [observed]
  remaining_percent        30.0 [observed] (account-wide, whole percentage points)
  window                   10080 minutes (168h00m)
  resets_at                2026-08-20T18:32:49+00:00
  observed_at              2026-08-15T07:02:25.846000+00:00
  staleness                110h02m old
  credits_balance          0
  hard stop                True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  plan_type / limit_id     prolite / codex
  lab's share of that      [unavailable]
  quota snapshots          67 [observed]
  paid trials seen         9 [observed]

cursor-cli
  remaining allowance      UNKNOWN [unavailable]
    reason: no paid trial in the scanned job directories recorded a provider quota snapshot, so the remaining allowance is unknown
    UNKNOWN is not 'plenty left'. This says the allowance could not be measured, not that a run fits inside it. Check the provider yourself before authorising anything billable.
  paid trials seen         0 [observed]
  quota snapshots          0 [observed]

  lab refusal ceiling      unset, so no lab ceiling refuses anything (reason code subscription_quota_ceiling)
    A lab ceiling is a spend decision and is recorded under its own reason code, never as the provider's statement.

QUEUE BY PURPOSE (states: proposed, pending, approved, waiting, running)
  nothing queued

POWER WARNINGS (queued comparisons only)
  none: no comparison is queued, so no power warning applies

VERDICT: nothing in these readings refuses, but codex has no overflow credits, so exhausting the window is a lockout until it resets, not an extra charge
```

## Mutation testing evidence

### Mutation 1: Drop new lanes from `PAID_AGENTS` (`PAID_AGENTS = frozenset({"codex", "claude-code"})`)
```
FAILED tests/test_quota.py::test_only_paid_agents_enter_the_ledger - AssertionError: assert {'claude-code', 'codex'} == {'antigravity... 'cursor-cli'}
  Extra items in the right set:
  'cursor-cli'
  'antigravity-cli'
FAILED tests/test_preflight.py::test_preflight_renders_all_four_lanes - AssertionError: assert 'antigravity-cli' in ...
Restored -> 2 passed
```

### Mutation 2: Ignore `cursor-cli` in `_trial_consumption`
```
FAILED tests/test_quota.py::test_cursor_and_antigravity_consumption_and_headroom - AssertionError: assert 0 == 1
  where 0 = len(())
Restored -> 1 passed
```

### Mutation 3: Fabricate observed headroom when no snapshots exist
```
FAILED tests/test_quota.py::test_cursor_and_antigravity_unobserved_headroom_gives_honest_reason - AssertionError: assert 'observed' == 'unavailable'
  - unavailable
  + observed
Restored -> 1 passed
```

## Premerge validation

```bash
env -u EVALLAB_DERIVED_ROOT bash scripts/premerge.sh
echo $?
```
Output:
- `1522 passed, 2 skipped, 1 xfailed`
- `SMOKE PASS both-stores-agree`
- `premerge green: Python 3.12; ty 27 <= 28`
- Exit code: `0`
