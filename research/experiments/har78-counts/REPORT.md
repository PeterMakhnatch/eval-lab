# HAR-78: Deterministic Counts Verdict and Campaign Rollup

**Date**: 2026-09-30  
**Schema**: `evallab.counts/v1`  
**Rule**: `reward` is never rewritten. `counts.verdict` is one of `counted_pass`, `counted_fail`, or `excluded`. Only deterministic facts exclude a trial. First failure, blame, and loops are attached for display and never decide.

---

## 1. Schema

```json
{
  "schema": "evallab.counts/v1",
  "raw_reward": 1.0,
  "scored": true,
  "verdict": "excluded",
  "reasons": ["copied_fix", "pass_tainted"],
  "evidence": [
    {
      "reason": "copied_fix",
      "detector": "upstream_fetch",
      "command": "cd /testbed && pip download waitress==2.0.0 --no-deps -d /tmp/wtr ...",
      "path": "agent/trajectory.json"
    }
  ],
  "flags": [],
  "judgments": [
    {
      "label": "first_failure",
      "value": "R-NONE-01",
      "attribution": null,
      "accuracy": null,
      "accuracy_note": "display only; HAR-119 pending; never decides"
    }
  ]
}
```

### Exclusion reasons (exact enum)

| Reason | Condition |
|---|---|
| `copied_fix` | `reward >= 1.0` and upstream package download detector fired |
| `pass_tainted` | `reward >= 1.0` and upstream fetch or verifier guard reject |
| `task_not_usable` | Hand label (`broken`, `suspect`, `discarded`, `review`, `unchecked`) or census row (`broken_environment`, `grader_suspect`, `unknown`) |
| `infra` | Not scored, verifier reward missing, or proxy 502 / bad gateway |

A fetch on a failure stays `counted_fail` with an informational flag. A budget exhaustion on a scored trial stays `counted_fail`.

---

## 2. Campaign Rollup (re-classified from landed trials)

| Campaign | Trials on disk | Raw Pass | Counted Pass | Counted Fail | Excluded | Excluded Breakdown |
|---|---|---|---|---|---|---|
| **HAR-81** | 44 | 8 | 8 | 36 | 0 | none (all 44 tasks sound, no answer-leak blocklist bypass) |
| **HAR-104** | 10 | 4 | 2 | 4 | 4 | 2 `copied_fix` (`000226`, `000927`), 2 `task_not_usable` (`002259`, `002407`) |
| **HAR-110** | 41 | 3 | 1 | 21 | 19 | 15 `infra` (proxy 502 / timeout), 2 `copied_fix`, 2 `task_not_usable` |

### Key takeaways for the board

- **HAR-81's 8/44 holds**: none of the 8 passes downloaded packages from PyPI, and none of the 44 tasks are broken in the census.
- **HAR-104's 4/10 is actually 2/6**: 2 passes copied the released package via `pip download`, and 2 failures were on tasks with hidden test requirements unstated in instructions (`002259`, `002407`). Counted pass rate is 33.3%, not 40%.
- **HAR-110's rate was depressed by gateway drops**: 15 of 41 landed runs were proxy/gateway drops (`BadGatewayError` / timeout) before verifier completion; 2 passes were copied; 1 pass was genuine (`har110-dev-002864-cfe31418__g7pfCqC`).

---

## 3. HAR-113 Variant & Census Structural Audit

Script: `research/experiments/har78-counts/audit_variants.py`  
Output: `research/experiments/har78-counts/variant-audit.json`

- **Variant records checked**: 247 `leak_variants` + 53 `repair_variants`.
- **Modified files allowlist**: all 300 variants modify only `environment/` files (blocklist, setup script) and `task.toml`. **0 findings** touching `tests/`, `verifier/`, or `instruction.md`.
- **Nop verification**: 52 repair variants had recorded nop jobs on disk; all 52 scored `0.0` clean.
- **Census state**: 1180 tasks total; 1040 `sound`, 135 `broken_environment`, 5 `grader_suspect`. 1175 graded nop trials scored `0.0`, 5 had unreadable rewards (reported as unchecked).
