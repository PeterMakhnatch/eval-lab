Status: building
Last: completed cycle 1 audit of preflight (CONFIRMED)
Next: cycle 2 audit of storm (agents/handoffs/storm-status.md)
Blockers: none

# M015: LOOP-AUDIT Handoff

Auditing handoff claims across invisible-surface modules against origin/main.

## Ledger

| date | subject | handoff | verdict | evidence path | risk note |
|---|---|---|---|---|---|
| 2026-08-18 | preflight | `agents/handoffs/preflight.md` | CONFIRMED | `research/audits/evidence/preflight/` | Core runtime and contract tests pass (31 tests); operator docs in `docs/operations.md` omit manual command reference |

## Cycle Log
- Cycle 1 (2026-08-18): Audited `preflight` (`agents/handoffs/preflight.md`). Verdict: CONFIRMED. Runtime command `evallab preflight` works without network/billable calls, `tests/test_preflight.py` passes 31 tests, digest section is integrated. Board note filed for missing documentation in `docs/operations.md`.

## Evidence Transcript: Cycle 1 (`preflight`)

### Command 1: `uv run pytest tests/test_preflight.py`
```
...............................                                          [100%]
31 passed in 0.59s
```

### Command 2: `uv run evallab preflight`
```
evallab preflight — is it safe and sensible to run right now
generated_at: 2026-08-18T06:40:00.873366+00:00
repository:   /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit
quota roots:  /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit/runs, /Users/petermakhnatch/Developer/eval-lab/.worktrees/m015-audit/research/evidence/runs

PER-PROVIDER REMAINING QUOTA (scope: account, NOT the lab; provider-reported)

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
  staleness                71h37m old
  credits_balance          0
  hard stop                True
    no overflow credits: reaching 100% blocks every paid agent until the window resets, it does not incur an extra charge
  plan_type / limit_id     prolite / codex
  lab's share of that      [unavailable]
  quota snapshots          67 [observed]
  paid trials seen         9 [observed]

  lab refusal ceiling      unset, so no lab ceiling refuses anything (reason code subscription_quota_ceiling)
    A lab ceiling is a spend decision and is recorded under its own reason code, never as the provider's statement.

QUEUE BY PURPOSE (states: proposed, pending, approved, waiting, running)
  nothing queued

POWER WARNINGS (queued comparisons only)
  none: no comparison is queued, so no power warning applies

VERDICT: nothing in these readings refuses, but codex has no overflow credits, so exhausting the window is a lockout until it resets, not an extra charge
```
