# HAR-78: Deterministic Counts Verdict and Campaign Rollup

**Date**: 2026-10-01  
**Schema**: `evallab.counts/v1`  
**Contract**: `reward` is never rewritten. `counts.verdict` is one of `counted_pass`, `counted_fail`, or `excluded`. Only deterministic facts exclude a trial. First failure, blame, and loops are attached for display and never decide.

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

### Deterministic Exclusion Reasons

| Reason | Condition |
|---|---|
| `copied_fix` | `reward >= 1.0` and upstream package download detector fired |
| `pass_tainted` | `reward >= 1.0` and upstream fetch or verifier guard reject |
| `task_not_usable` | Hand label (`broken`, `suspect`, `discarded`, `review`, `unchecked`) or census row (`broken_environment`, `grader_suspect`, `unknown`) |
| `infra` | Not scored, verifier reward missing, or proxy 502 / bad gateway |

- Upstream fetch on a failure stays `counted_fail` (flagged `upstream_fetch`, decisive=false).
- A budget exhaustion on a scored trial stays `counted_fail`.
- Diagnostic judgments (`first_failure`, blame, loops) carry `accuracy: null` and never exclude.

---

## 2. Campaign Rollups

### A. HAR-81 (ARVO Cyber Tasks)

| Campaign | Trials on disk | Raw Pass | Counted Pass | Counted Fail | Excluded | Notes |
|---|---|---|---|---|---|---|
| **HAR-81** | 44 | 8 | **8** | 36 | 0 | **No ledger covers these tasks (not checked)**. All 8 passes earned; no PyPI package download bypasses. |

### B. HAR-104 (Plain Dev & Dropped Baseline)

| Trial | Task | Raw Reward | Counted Verdict | Reasons | Note |
|---|---|---|---|---|---|
| `har104-d-000226__JCDfZFi` | `format-code-task-000226` | 1.0 | `excluded` | `copied_fix`, `pass_tainted` | `pip download waitress==2.0.0` |
| `har104-d-000383__PmZMZ6z` | `format-code-task-000383` | 0.0 | `counted_fail` | - | Budget exhausted |
| `har104-d-000927__23aAzui` | `format-code-task-000927` | 1.0 | `excluded` | `copied_fix`, `pass_tainted` | `pip download soupsieve==1.9.1` |
| `har104-d-001832__d7Hop8E` | `format-code-task-001832` | 0.0 | `counted_fail` | - | Budget exhausted |
| `har104-d-001896__MDkTErY` | `format-code-task-001896` | 0.0 | `counted_fail` | - | Agent finished |
| `har104-d-002256__RDffvXQ` | `format-code-task-002256` | 0.0 | `counted_fail` | - | Budget exhausted |
| `har104-d-002259__cptLF6h` | `format-code-task-002259` | 0.0 | `excluded` | `task_not_usable` | Hidden test contract unstated |
| `har104-d-002391__WxBjcjX` | `format-code-task-002391` | 1.0 | `counted_pass` | - | Earned pass |
| `har104-d-002407__LRiiKmy` | `format-code-task-002407` | 0.0 | `excluded` | `task_not_usable` | Hidden test labels required |
| `har104-d-002864__B7cJ4cG` | `format-code-task-002864` | 1.0 | `counted_pass` | - | Earned pass |

**HAR-104 Rollup:** 10 trials | Raw Pass: 4 | **Counted Pass: 2** | **Counted Fail: 4** | **Excluded: 4**  
*True pass rate:* **33.3% (2/6)** on sound tasks.

---

### C. HAR-110: Rollup per Round and Arm (Matching `RESULTS.md`)

*Note on non-trial records:* The 13 `attempt-*` directories under `har110-live/runs/gepa-har110-python-train-search/` are optimizer search queue proposals with `status: pending_evaluation` (`EvaluationPending: evaluation ... already pending in queue`). No model executed in them. They are optimizer queue entries, not trials, and are dropped from the trial rollup.

There is **exactly one infra trial** in HAR-110: `002256` candidate (`har110-dev-002256-cfe31418__uDRp4Vb`), which failed on proxy 502 `BadGatewayError: OpenAIException - unsupported upstream encoding` after 251k tokens before the verifier ran.

#### 1. Development Split (6 Tasks)

| Task | Plain (HAR-104 Retained) | Seed Addendum (399ec113) | Candidate Addendum (cfe31418) |
|---|---|---|---|
| **000383** | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) |
| **001832** | 0.0 (`counted_fail`) | 1.0 -> **`excluded`** (`copied_fix`, `pass_tainted`) | 0.0 (`counted_fail`) |
| **001896** | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) [parked spec job `...1b2701`] |
| **002256** | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) | None -> **`excluded`** (`infra`: proxy 502) |
| **002391** | 1.0 (`counted_pass`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) |
| **002864** | 1.0 (`counted_pass`) | 0.0 (`counted_fail`) | 1.0 (`counted_pass`, agent finished early) |
| **Rollup (Raw)** | **2/6** | **1/6** | **1/5 (+1 unscored)** |
| **Rollup (Counted)** | **2/6 pass (4 fail)** | **0/5 pass (5 fail, 1 excluded)** | **1/4 pass (4 fail, 1 excluded)** |

#### 2. Held-Out Split (4 Tasks)

| Task | Plain | Seed Addendum (399ec113) | Candidate Addendum (cfe31418) |
|---|---|---|---|
| **000495** | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) |
| **000587** | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) |
| **001161** | 0.0 (`counted_fail`, flag `upstream_fetch`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) |
| **001181** | 0.0 (`counted_fail`, flag `upstream_fetch`) | 0.0 (`counted_fail`) | 0.0 (`counted_fail`) |
| **Rollup (Raw)** | **0/4** | **0/4** | **0/4** |
| **Rollup (Counted)** | **0/4 pass (4 fail)** | **0/4 pass (4 fail)** | **0/4 pass (4 fail)** |

*Observation:* Plain runs on `001161` and `001181` fetched PyPI upstream and still failed; under the HAR-78 contract, failed runs that fetch stay `counted_fail` with an informational flag (`upstream_fetch`, decisive=false).

#### 3. v1 Split Dropped Tasks (3 Tasks)

| Task | Plain (HAR-104 Retained) | Seed Addendum (399ec113) | Verdict / Reason |
|---|---|---|---|
| **000226** | 1.0 -> `excluded` | 1.0 -> `excluded` | `copied_fix`, `pass_tainted` (`pip download waitress`) |
| **002259** | 0.0 -> `excluded` | 0.0 -> `excluded` | `task_not_usable` (hidden test contract) |
| **002407** | 0.0 -> `excluded` (flag `fetch`) | 0.0 -> `excluded` (flag `fetch`) | `task_not_usable` (hidden test labels) |

---

## 3. Structural Audit of Task Variants & Census State

Script: `research/experiments/har78-counts/audit_variants.py`  
Output: `research/experiments/har78-counts/variant-audit.json`

### Audited Variant Sets (344 Total)
1. **HAR-113 Leak Variants** (`leak_variants.json`): 247 tasks (`leak-close-pypi@1`).
2. **HAR-113 Environment Repairs** (`repair_variants.json`): 53 tasks.
3. **HAR-115 Environment Repairs (PR #580)** (`har115-census/repairs.json`): 44 tasks.

### Audit Findings
- **Modified files allowlist**: All 344 variants modify only files under `environment/` (`setup/setup.sh`, `setup/files/blocklist`) and `task.toml`.
- **`task.toml` key modifications**: Exactly one key is modified across all 344 variants:
  ```toml
  [environment.healthcheck]
  command = "echo <base64> | base64 -d | tar -xzf - -C /var/lib/mimo && ..."
  ```
  This re-embeds the updated `environment/setup` folder into the inline payload that the `mimo_harbor` container runner actually unpacks and executes. All other tables (`[task]`, `[metadata]`, `[agent]`, `[verifier]`, container limits) are byte-for-byte identical.
- **Protected files**: **0 findings** touching `tests/`, `verifier/`, `solution/`, or `instruction.md`.
- **Nop verification**:
  - 96 repair variants had recorded nop validation runs on disk: all 96 scored `0.0` clean.
  - 248 variants unchecked (leak variants only add hosts to the blocklist without changing tests).
  - **0 passed nops**.

### Python Census State (`task_health.parquet`, 1,180 Tasks)
- **sound**: 1,040
- **broken_environment**: 135
- **grader_suspect**: 5
- **nop_reward == 0.0**: 1,175
- **nop_reward is None** (unscorable/setup error): 5
