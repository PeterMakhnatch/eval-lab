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
| `copied_fix` | `reward >= 1.0` and confirmed upstream acquisition with recorded, target-bound outcome evidence |
| `pass_tainted` | `reward >= 1.0` and confirmed upstream acquisition or verifier guard reject |
| `task_not_usable` | Digest-matched canonical ledger status `review`, `discarded` or `unchecked`; without a match, the legacy hand/census exclusion policy below |
| `infra` | Not scored, verifier reward missing, or proxy 502 / bad gateway |

- Confirmed upstream acquisition on a verifier failure stays `counted_fail` (flagged `upstream_fetch`, decisive=false). Failed or unknown fetch attempts never decide counts, including on a native pass; they remain `upstream_fetch_attempt` flags.
- A budget exhaustion on a scored trial stays `counted_fail`.
- Diagnostic judgments (`first_failure`, blame, loops) carry `accuracy: null` and never exclude.

### Successful acquisition evidence (HAR-131 correction)

`upstream_fetch.detect_upstream_fetch` remains the canonical command-only
**attempt** matcher. `process-job` now adds an outcome (`succeeded`, `failed`,
or `unknown`) and source-bound evidence. Both copied-fix and fetch-based
pass-taint exclusions require a saved/listed exact artifact followed by bound
unpack or source-read proof. Command-only, bare-status, and summary-only flags
have no successful-acquisition fallback. The separate
opt-in GEPA `UPSTREAM_FETCH_ZERO` objective is deliberately unchanged: **any
attempt** still zeros that objective. Do not substitute its policy for counts.

The Research-Harbor 10:53Z frozen correction requires **successful upstream
acquisition**: an exact pinned package/version artifact saved or listed in the
fetch destination, then observed being unpacked **or** read. A package-specific
summary or exit code 0 alone cannot decide counts. Call results must match the
recorded call ID inside the same document and step. A legacy untagged result is usable only
when an explicit, unique native-call command echo bounds its terminal window;
the evidence records `terminal-command-window` attribution rather than
inventing a `source_call_id`. Buffered text before the current command echo
and output after the next prompt are not current-call stdout. Echoes and
literal command content cannot prove success. Artifact-use proof stops at
another attempt for the same normalized package/target or a document boundary.
Another package, path, episode, continuation, or reused numeric step ID cannot
supply the proof.

Artifact proof spans saved/listed artifact → observed unpack or source read.
Other output-producing commands in a fetch call do not invalidate an observed
exact saved artifact **when** its subsequent use proves the chain. Unpack proof
requires observed extracted Python files, with the matching listing gated by
the exact extraction command's `&&` success; listing a preexisting destination
after `;` or `|| true` is not proof. A source reader following extraction must
likewise be success-conditioned. Relative paths require an explicit
`cd ... &&` binding; directory spelling is normalized lexically (`/`, `./`,
`..`) without probing the filesystem. A wheel glob may vary only after the
complete observed package/version prefix, in the exact artifact directory;
`*.whl`, wildcard versions, and other directories cannot supply proof.

Missing/empty output, merely seeing a URL/filename, a successful local import,
and no observed error remain **unknown**. Informational index queries, remote
configuration, unparsed commands, and script fetches without bound acquisition
proof are not promoted by a whole-script or isolated-fetch exit status.
Recorded nonzero status can establish an isolated fetch's failure, not success.
Conservative unknowns are exposed, not fabricated failures or successes.
An independent guard reject, unusable-task decision, or infra exclusion still
applies. Native rewards and experimental evidence remain immutable.

Portable producer→counts regressions retain exact native excerpts under
`tests/fixtures/upstream_fetch/`, with trajectory/result SHA-256 provenance:
001870 (`0c82546a26b004d5e3e544deada7273a08c70ce2edc176d85ed88d52dd3c47b3`)
has only unsuccessful/unconfirmed markdownify attempts and is no longer
fetch-excluded; 000341
(`9e03e80486254e608b921fccc52505c9a5a076f42a13d3425ca7e94b860e9209`)
retains the step-40 exact wheel listing plus step-41 observed unpack/read
proof and remains fetch-excluded. Both native rewards are 1.0. These statements
concern the fetch exclusions, not independent task-usability/guard decisions.

Native R2 boundary fixtures preserve additional exact excerpts:

- 001373-a2-r2
  (`5f39297af38303452ab9c6fdb75948393de6e12b79976c8bc8735389c30259e1`):
  a preceding `git log` shares the fetch call, but the exact saved
  google-cloud-logging artifact and its subsequent unzip/source read confirm
  acquisition.
- 002356-a1-r2
  (`1ae12d27a0c5ad24967e8a839c1b91369d1b730c61d8ab64b9944af6fe8aa9ec`):
  the exact black 24.4.2 wheel listing, pinned platform-suffix unzip glob,
  and success-conditioned extracted-file listing form a two-call acquisition
  proof. The separate native `numerics.py` read is retained but is not required
  after observed successful unpack.
- 001269-a2-r2
  (`1ff9c912062f9b39a31c7d54a4a8566c0368e4c9fa66c18df7e13fbdbd1b3da4`):
  the responses download attempt has no immediate output; the next local-read
  window contains `Could not find a version` / `No matching distribution`
  errors for that target. The fetch remains a visible, non-deciding
  unknown-attribution attempt. This is **not** a clean-trial claim: the native
  run separately reads a preexisting `build/lib` donor and lands its exact
  changes. That independent local-copy evidence must not be represented as
  successful pip acquisition. Under the frozen correction it is **not a counts
  route**: Data's task/variant-usability downgrade must exclude the leaked image.
  These fetch-scoped fixture results do not substitute for that usability
  dependency or claim this native trial is counted.



### Task-version binding (HAR-131)

`process-job` supplies the retained `experiment-spec.json` task ID and
`task_package_digest`. A row in `python-task-ledger/ledger.csv` is authoritative
only when its `run_digest` matches that exact package. A matched `review`,
`discarded` or `unchecked` row excludes it. The validated-variant rule from
HAR-127/#600 is retained: a validated same-task repair can lift the original
package's census exclusion. Hand exclusions still apply even when the ledger
row says `usable`; candidate or wrong-task variants do not lift the exclusion.

For historical jobs with no digest, a different digest, or no ledger row, the
existing policy is unchanged: hand `broken`/`suspect`/`discarded`/`review`/`unchecked`
or census `broken_environment`/`grader_suspect`/`unknown` excludes, with the
validated-variant exception above. Missing evidence alone is not a ledger status
and does not invent a new exclusion.

`counts.task_status` reports the applied `status` (null without a match), the
unapplied `ledger_status`, task and package identities, `digest_match`, the CSV
SHA-256, source path and supporting evidence. A mismatched current ledger is not
proof that an old run used a repaired or usable task. The page renders this
binding rather than claiming every task is unchecked. Rollups below retain their
historical measurement; regenerate before using them as current cohort counts.


---

## 2. Campaign Rollups

HAR-132's read-only backfill replay reproduced **90 comparison cells from
81 distinct raw trials**, with no keyed row differences. Nine HAR-104 trials
are reused in HAR-110 cells; summing campaign cells is not a unique-run count.

### A. HAR-81 (mixed-domain declared cohort)

| Campaign | Trials on disk | Raw Pass | Counted Pass | Counted Fail | Excluded | Notes |
|---|---|---|---|---|---|---|
| **HAR-81** | 44 | 8 | **8** | 36 | 0 | **No ledger covers these tasks (not checked)**. Eight pass under the recorded counts rules; absence of detector findings is not independent proof that every pass is earned. |

The frozen `round_arm=arvo_cyber` value is a legacy grouping label, not a
domain assertion about all 44 tasks.

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
*Counted pass rate:* **33.3% (2/6)** among non-excluded trials.

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
| **Rollup (Counted)** | **2/6 pass (4 fail)** | **0/5 pass (5 fail, 1 excluded)** | **1/5 pass (4 fail, 1 excluded)** |

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
