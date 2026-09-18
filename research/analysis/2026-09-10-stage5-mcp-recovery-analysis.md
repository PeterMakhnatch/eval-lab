---
status: needs_human_review
audience:
  - analyst
  - operator
---

# Stage 5 — MCP single-fault recovery analysis

Date: 2026-09-10. Benchmark: `mcp-recovery-v1`. This is read-only analysis of completed evidence, not a new experiment, regrade, or capability estimate.

## Findings at a glance

- **All seven fault-bearing trajectories detect the anomaly and eventually confirm an exact write/read match.** Five detect explicit error text; two detect a wrong value despite a successful tool response.
- **All seven repeat the original write arguments unchanged.** Adaptation is an intervening tool/strategy change, not a changed key or value. No observed fault trajectory is an immediate, diagnosis-free repeated write.
- **Recorded recovery reward is 6/7, not 7/7.** The single zero-reward trajectory, `recovery__HYxt7du`, detects its 502 and restores the record, but a diagnostic read of a missing key precedes fallback and closes the verifier's strategy-mutation window. This is a **candidate verifier false negative / contract-validity problem**, not evidence that the model missed the error or failed to recover operationally.
- **Seven clean controls pass 7/7 with two tool calls each.** Four are hidden behind truncated directory names beginning with the fault class. Counting all eight wave-2 `recovery-*` directories as fault trials would double the fault denominator.
- Silent-data trials take longer than the 403 cases in this small cohort. The model detects corruption correctly, but one final explanation credits credentials without isolating that cause.

## Scope, retrieval and validity

The assigned `runs/mcp-rec-*` and `runs/zai-wave2-flash-matrix/recovery-*` paths do not exist in the current primary checkout. `mcp-rec-<id>` survives as the **task identifier**, not the discovered job-directory prefix. The requested conditions are available as promoted evidence under `research/evidence/runs/`, with originals still at the `trial_uri` paths in `.worktrees/zai-opencode-experiments/runs/`. No original/promoted duplicate is counted twice.

Included, without outcome selection:

1. `zai-flash-recovery-clean-twin-r3-amd64-verifier`: all 3 trials.
2. `zai-flash-recovery-transient5xx-p1-r3-amd64-verifier`: all 3 trials.
3. `zai-wave2-flash-matrix`: all 8 recovery trials (4 fault arms and 4 clean twins).

The additional `zai-wave2-glm53-paired-mini` recovery trial is a different-model job outside the named flash matrix and is excluded. Earlier canary/setup variants found alongside the original jobs are also outside these selected finalized jobs; no claim is made about all historical recovery launches. There were **14/14 analyzable selected trials, 0 missing selected trajectories, and 0 recorded trial exceptions**.

All selected results identify OpenCode **1.18.25**, adapter `zai_opencode_adapter:ZaiOpenCode`, model `zai-coding-plan/glm-5.3-flash`, Docker, and separate verification. Wave-1 configured concurrency is 1; wave-2 is 3. Wave-2 lock names identify seeds 42 and 1337 and clean/fault arms. Every fault checks file records exactly one required/observed injection. Input/output token counts are retained in the structured facts; reported cost is null, **not zero**.

### Evidence checks actually run

- Verified **163 promoted-file SHA-256 digests** against the three `PROMOTION.json` manifests: zero mismatches. This includes selected trial files plus the jobs' top-level promoted files; unrelated wave-2 trials were not included in this digest count.
- Verified **42 original-file digests** (result, trajectory, canonical event journal for each trial) against each promotion manifest's source digest: zero mismatches. Redacted promoted trajectories are not falsely compared byte-for-byte with unredacted originals.
- Parsed all 14 results, trajectories, locks, checks, reward JSONs and canonical event journals. Verified completion, sequential ATIF step IDs and event ordinals, agreement of reward/checks/result, and exact equality of MCP tool-name/arguments ordering between ATIF and the canonical event journal. Every final matching read was independently checked against the first write's key/value context.
- No project tests, linters, paid calls, task launches, or source-evidence edits were performed.

**Limits:** this is structural ATIF validation, not validation against an installed ATIF-v1.7 JSON schema. Five explicit error results are absent as ATIF `observation` entries; their error strings and ordering are preserved in `artifacts/app/output/benchmark-events.jsonl`, and the next agent messages acknowledge them. This is a logging limitation, not missing fault exposure. Silent corruption is logged as `tool_call_success`, `is_error:false`; an error-code-only extractor misses it.

Task lock digests and result task checksums are preserved separately, without assuming their algorithms are interchangeable. Exact deployed task/verifier fixtures and Harbor version are not independently established from the retained bundle. The original experiment worktree retains a verifier implementation matching these legacy checks fields, but its deployed-source binding has not been proven. No sealed envelope was decrypted and no Oracle/NOP controls were rerun or independently certified. Clean model twins are **not** substitutes for Oracle/NOP validity controls. These provenance limits preclude a fully certified causal/model-ranking claim.

## Deterministic comparison

An ATIF total includes the initial user step and final answer. `LLM calls` is the sum of `llm_call_count`. A tool call means one ATIF call; MCP calls are additionally checked against the canonical journal. `Recovery steps` excludes the fault-bearing step and ends at the first subsequent matching read, not the final prose answer. Thus fault at step 2 and confirmation at step 5 means **3** subsequent steps. For clean trials, recovery is not applicable, not zero-latency recovery.

| Condition | Arms inspected | Recorded reward passes | Operational matching read | Total ATIF steps | Total tool calls |
|---|---:|---:|---:|---|---|
| Clean twins, pooled | 7 | 7/7 | 7/7 | 4 each | 2 each |
| `recovery-transient5xx`, p=1 | 3 | 2/3 | 3/3 | 6, 6, 8 | 5, 4, 7 |
| `recovery-persistent-signature-error`, fault arms | 2 | 2/2 | 2/2 | 6, 6 | 4, 4 |
| `recovery-silent-wrong-payload`, fault arms | 2 | 2/2 | 2/2 | 7, 9 | 5, 7 |

The original clean-twin job is 3/3. The two additional signature clean twins and two silent clean twins are each 2/2. Overall recorded reward is 13/14; fault-only reward is 6/7. Do not turn those tiny, differently seeded/configured cohorts into a population recovery rate.

### Every selected trial

`E→R` gives fault step → matching read step; `post-E MCP` excludes the fault call and includes the confirming read. `—` means no fault. Source links lead to promoted trial directories; append `agent/trajectory.json`, `verifier/checks.json`, or `artifacts/app/output/benchmark-events.jsonl` for the cited evidence.

| Trial | Lock condition | Reward | Steps / LLM calls | Tools / MCP | E→R | Recovery steps / post-E MCP |
|---|---|---:|---|---|---|---|
| [recovery-clean__2Li2u7z](../../research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__2Li2u7z/) | `recovery-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery-clean__Hj7RGKk](../../research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__Hj7RGKk/) | `recovery-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery-clean__PbWN3P6](../../research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__PbWN3P6/) | `recovery-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery__HYxt7du](../../research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__HYxt7du/) | `recovery` | 0 | 6 / 5 | 5 / 5 | 2→5 | 3 / 4 |
| [recovery__ari74b9](../../research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ari74b9/) | `recovery` | 1 | 6 / 5 | 4 / 4 | 2→5 | 3 / 3 |
| [recovery__ozALVY2](../../research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ozALVY2/) | `recovery` | 1 | 8 / 7 | 7 / 5 | 2→7 | 5 / 4 |
| [recovery-persistent-signature-er__Lm9ohzb](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__Lm9ohzb/) | `recovery-persistent-signature-error-s42-fault` | 1 | 6 / 5 | 4 / 4 | 2→5 | 3 / 3 |
| [recovery-persistent-signature-er__QHaoyPA](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__QHaoyPA/) | `recovery-persistent-signature-error-s1337-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery-persistent-signature-er__YXTpcLZ](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__YXTpcLZ/) | `recovery-persistent-signature-error-s1337-fault` | 1 | 6 / 5 | 4 / 4 | 2→5 | 3 / 3 |
| [recovery-persistent-signature-er__mp4YbvW](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__mp4YbvW/) | `recovery-persistent-signature-error-s42-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery-silent-wrong-payload-s1__UBdmtRU](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__UBdmtRU/) | `recovery-silent-wrong-payload-s1337-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery-silent-wrong-payload-s1__fZmgR5y](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__fZmgR5y/) | `recovery-silent-wrong-payload-s1337-fault` | 1 | 7 / 6 | 5 / 5 | 3→6 | 3 / 3 |
| [recovery-silent-wrong-payload-s4__PmKJB8T](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__PmKJB8T/) | `recovery-silent-wrong-payload-s42-clean` | 1 | 4 / 3 | 2 / 2 | — | — |
| [recovery-silent-wrong-payload-s4__cVZm97U](../../research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__cVZm97U/) | `recovery-silent-wrong-payload-s42-fault` | 1 | 9 / 8 | 7 / 7 | 3→8 | 5 / 5 |

## Exact step-level recovery traces

Tool names below omit the `mcp-service_` prefix. `S` is the ATIF step ID; `E` is the 1-indexed canonical event ordinal. Multiple tools in one ATIF step need event ordering, not an invented ordering of model thoughts.

### Clean twins: no recovery opportunity

All seven: **S2/E1** `write_record` succeeds; **S3/E2** `read_record` returns the identical nonempty value with `exists:true`; **S4** final answer confirms the match. There is no retry, fallback or auth refresh. The wave-2 lock names, rather than truncated directory names, identify `QHaoyPA` and `mp4YbvW` as signature **clean** arms and `UBdmtRU` and `PmKJB8T` as silent **clean** arms.

### Transient 502 — `recovery__ari74b9`: shortest accepted repair

1. **S2/E1**, call `call_593b04287b7444fc8ef80310`: write `{"key":"recovery-check-2026-08-29","value":"operational-recovery-ok-42"}`. Canonical error: `502 Bad Gateway: downstream service returned invalid payload`.
2. **S3/E2**, call `call_60f6eb77d0ec4ea29dfb8e27`: explicitly identifies the 502 and invokes `refresh_auth({"scope":"mcp-service"})`; response is `authenticated`.
3. **S4/E3**: repeats the **identical** write payload; success.
4. **S5/E4**: reads the same key; exact value match. **S6** summarizes.

Detection latency is one subsequent ATIF step. Repair tool selection changes; write arguments do not. Matching-read recovery takes **3 steps / 3 MCP calls** after the fault. Checks: `causal_mutation:true`, `auto_clear:false`, reward 1. The claim that 502 specifically indicates stale credentials is a model hypothesis; 502 text alone does not establish that root cause.

### Transient 502 — `recovery__ozALVY2`: longer diagnostic detour

1. **S2/E1**, call `call_a75565de4e0340a88afe8c22`: write `{"key":"recovery-check","value":"operational-recovery-ok"}`; same 502 error.
2. **S3** explicitly detects 502. **E2** calls `fallback_query({"query":"status"})` → `synchronized`, alongside a `bash` HTTP initialize probe. The probe returns HTTP 200 and `mcp-recovery-sidecar` version `3.4.7`; this establishes reachable transport, not successful record storage.
3. **S4** uses a second `bash` call to initialize/list tool schemas. The observation lists `write_record` with required string `key` and `value`. Although the message proposes a direct HTTP write, the canonical journal records **no extra record write here**.
4. **S5/E3**, call `call_1524c7ce87b84384b36aefb1`: `refresh_auth({"scope":"operational-store"})` → `authenticated`.
5. **S6/E4** repeats the identical write; **S7/E5** confirms an exact matching read; **S8** final answer.

Recovery takes **5 subsequent ATIF steps**, **4 subsequent MCP calls**, and **6 subsequent total tool calls** (including two bash diagnostics). Checks reward 1 and causal mutation true. The successful trajectory does not show that all diagnostic calls were necessary, or isolate fallback versus auth versus transient exhaustion as the causal reason it recovered.

### Transient 502 — `recovery__HYxt7du`: recovered state, rejected score

1. **S2/E1**, call `call_d7ecb7b789a04a3195163971`: write `{"key":"recovery-test-2026","value":"operational-recovery-ok"}`; canonical 502 error.
2. **S3** says the write returned a 502 and checks whether it landed. It submits two calls in the same model step:
   - **E2**, `call_3778e6499b33475ab1484e7c`: `read_record({"key":"recovery-test-2026"})` → `exists:false`, `value:null`.
   - **E3**, `call_63b1dc3138a543ceb323b737`: `fallback_query({"query":"SELECT * FROM records WHERE key = 'recovery-test-2026'"})` → `synchronized`.
3. **S4/E4**, call `call_43f06b1ff82c43ea822e1bab`: repeats the identical write, now successful.
4. **S5/E5**, call `call_a6f1aaa844f44baaa732b0a6`: independent read matches exactly. **S6** accurately reports the operational roundtrip, but does not know the hidden score.

Recovery takes **3 steps / 4 MCP calls** after the fault. This is **not a blind same-payload retry**: it diagnosed non-persistence and invoked fallback before the retry. Checks nevertheless say `causal_mutation:false`, `auto_clear:true`, `read_confirmed:true`, `state_changed:true`, `write_ok:true`, reward 0.

The original experiment worktree's `library/benchmarks/mcp-recovery-v1/verifier_core.py:129–156` accepts either `write_record` **or `read_record` with outcome `ok`** as the first post-fault target success. It requires a mutation strictly before that success. Its `:202` sets `auto_clear` from state change, exact injections and absence of an eligible mutation. A missing-key read is a successful transport/tool event. Therefore the recorded ordering is fault **E1**, target success **E2**, mutation **E3**: the strict interval `1 < mutation < 2` is empty. A local replay of this ordering predicate returned false, matching the checks.

**Interpretation:** the earliest score-disqualifying event is the diagnostic read at **S3/E2**, not failure to notice S2's error. The legacy implementation accepts either mutation tool, so missing `refresh_auth` alone is not a sufficient explanation of this legacy zero. The surviving main-checkout verifier now has additional designated-repair constraints; those must not be retroactively applied to this old result.

**Stage-5 classification:** candidate `verifier_false_negative`, medium confidence. The narrow hidden ordering rule may intentionally define the benchmark construct, in which case this is `tool_use`/ordering failure *under that contract*, not a scoring implementation error. But it rejects operationally useful diagnosis and successful task completion, so it is inappropriate to interpret this zero as failed recovery capability. Whether the benchmark's intended construct should forbid this diagnostic read remains a human-review question. The original deployed verifier snapshot was not recovered with a matching source digest, so this explanation is strongly corroborated rather than a certified regrade.

### Persistent signature 403 — two direct auth repairs

| Fault trial | Exact initial/retried write arguments | S3 auth scope |
|---|---|---|
| `recovery-persistent-signature-er__Lm9ohzb` (seed 42) | `{"key":"recovery-check-20260829","value":"operational-recovery-ok"}` | `records` |
| `recovery-persistent-signature-er__YXTpcLZ` (seed 1337) | `{"key":"recovery-test","value":"hello-mcp-recovery"}` | `mcp-service` |

Both follow the same observed sequence:

- **S2/E1**: `write_record` → `403 Forbidden: authentication signature invalid or credentials expired`.
- **S3/E2**: explicitly identifies invalid/expired credentials and calls `refresh_auth` → `authenticated`. Auth call IDs are `call_3d6b5185bf3c44a8a9ce744e` and `call_ac80d104375e45fbb3624f7a` respectively.
- **S4/E3**: identical write succeeds.
- **S5/E4**: independent same-key read matches. **S6** final answer.

Each detects in the next step and recovers in **3 steps / 3 MCP calls** after the error. Both checks files record one injection, causal mutation true and reward 1. Both paired clean arms use 4 ATIF steps/2 tools: the fault arms add 2 steps and 2 tools. These are two seed-paired observations, not repeated estimates of a general persistent-fault recovery probability.

### Silent wrong payload — `recovery-silent-wrong-payload-s1__fZmgR5y`

1. **S2/E1** writes `{"key":"recovery-check","value":"recovery-ok-2026-08-29"}` → success.
2. **S3/E2**, call `call_882d5d8848b14c1f938935ad`: `read_record` returns `corrupted_stale_cache_record`, `exists:true`, without any error code.
3. **S4/E3**, call `call_b42dfd32ef154aa2afb764b2`: explicitly notices the value mismatch and calls `fallback_query({"query":"status key=recovery-check"})` → `synchronized`.
4. **S5/E4** rewrites the identical original payload; **S6/E5** reads the correct value; **S7** concludes.

Detection is at the next step, based on **semantic value equality**, not a transport flag. Recovery takes **3 steps / 3 MCP calls** after the bad read. Checks reward 1, causal mutation true, read confirmed true. This passing silent-read case uses **no auth refresh**, a concrete counterexample to interpreting credential refresh as a universally necessary repair. The assertion that an old record existed under the key is conjecture, not direct evidence.

### Silent wrong payload — `recovery-silent-wrong-payload-s4__cVZm97U`

1. **S2/E1** writes `{"key":"recovery-check","value":"operational-recovery-ok"}` → success.
2. **S3/E2**, call `call_55fcd403ed50417dbbf43444`: read returns `corrupted_stale_cache_record`, `exists:true`, success status.
3. **S4/E3** explicitly detects mismatch; `fallback_query({"query":"status key=recovery-check"})` → `synchronized`.
4. **S5/E4** asks fallback for `get key=recovery-check`; it again returns only synchronized status and echoes the query, not the record value.
5. **S6/E5**, call `call_582aad5857bb4a2a8cb01286`: notes the fallback's limited answer and refreshes auth with scope `operational-store`.
6. **S7/E6** rewrites the identical original payload. **S8/E7** reads the correct value. **S9** final answer attributes the issue to stale credentials.

Recovery takes **5 steps / 5 MCP calls** after corruption. Reward 1; causal mutation and read confirmation true. **Observed:** mismatch detection and successful restoration. **Unsupported attribution:** the final answer credits credentials, but there is no intervening read after the first fallback to show that corruption persisted. Fallback was already sufficient in the other seed's passing trace; extra auth here is not an isolated cause. Tag `evidence_use` only for this causal overstatement, not for task failure.

Both silent fault checks files report `write_ok:false` alongside `success:true` and reward 1. Do not infer an absent/failed write: the journal contains two successful writes. The legacy verifier leaves `post_mutation_writes` empty in its read-target branch (`verifier_core.py:159–178`) and returns that list's truth value as `write_ok` (`:209`). This is component-field semantics, not a contradicted successful roundtrip. No verifier false positive is established for these exact-match trajectories.

## Stage-5 synthesis, alternatives and smallest discriminator

| Rubric question | Evidence-backed disposition |
|---|---|
| Valid attempt or infrastructure failure? | All 14 selected attempts complete, have live tool interactions and no trial exception. Injected 502/403 are benchmark events, not proxy-auth trial-launch failures. Provenance certification remains partial. |
| Invalid shortcut accepted / valid result rejected? | No shortcut demonstrated; exact read equality is observable in every trial. `HYxt7du` is a candidate rejection of valid operational recovery because diagnostic-read ordering preempts the mutation window. |
| Earliest failure point? | Sole zero score: S3/E2 missing-key diagnostic read, after detected S2/E1 fault. Semantic overclaim in `cVZm97U`: S9 credential attribution unsupported by isolation. |
| Failure taxonomy? | One candidate `verifier_false_negative` (alternative: intended `tool_use` ordering contract). One `evidence_use` annotation on a successful trial's explanation. Remaining 12 have no demonstrated failure mechanism; structured category `unknown` must not be counted as 12 failures. |
| Direct evidence? | Per-trial trajectories, exact canonical event ordinals and call IDs above; recorded checks and hashed provenance below. |
| Alternatives? | Intentional strict mutation ordering; auto-exhaustion versus causal repair for transient faults; fallback versus auth in overdiagnosed cases; persistence semantics and unverified deployed-source revision. |
| Smallest distinguishing experiment? | Free deterministic verifier-contract replay of event ordering with fixed task and final state, followed only if needed by an approved paired model experiment. |

**Proposed discriminator (not executed):** recover the exact task/verifier fixtures identified by the stored digests. Use local fixed-policy controls to compare `fault → missing-key read → fallback → identical write → matching read` with `fault → fallback → missing-key read → identical write → matching read`. The only intended variable is whether a non-mutating diagnostic read precedes fallback. Fix seed, injected count, payload and final state; serialize calls to remove scheduling ambiguity. Two local attempts, concurrency 1, 60-second timeout per control, paid-model cost ceiling **$0**; stop on provenance mismatch, nonidentical final states or missing event evidence. Run a clean Oracle and NOP against those exact fixtures as validity controls, not capability samples. If scoring changes solely with diagnostic ordering, review whether that ordering defines the intended construct. If scoring does not reproduce, investigate source/runtime drift before drawing a model conclusion. Execution is a separate action and requires the exact fixtures; this report does not authorize a model/GPU/cloud run.

A second, separate optional discriminator for silent corruption is `fallback → immediate read` versus `fallback → refresh_auth → read`, with no intervening rewrite. It isolates whether the additional auth move contributes; it must not be combined with the ordering experiment. The present evidence is insufficient to attribute all accepted recoveries to the model's stated root-cause explanations.

## Reproduction and structured Stage-5 sidecars

The following Python extraction sketch is read-only. Its selection rule and metrics reproduce the key table directly from promoted artifacts, independent of PostgreSQL/Parquet feature availability. MCP journals recover the five omitted ATIF error observations. The source-task `mcp-rec-*` IDs and digests below provide joins to derived tables; no stale all-null feature column is treated as proof of no error.

```python
import json
from pathlib import Path
root = Path('/Users/petermakhnatch/Developer/eval-lab/research/evidence/runs')
jobs = [
    'zai-flash-recovery-clean-twin-r3-amd64-verifier',
    'zai-flash-recovery-transient5xx-p1-r3-amd64-verifier',
    'zai-wave2-flash-matrix',
]
for job in jobs:
    for path in sorted((root / job).glob('recovery*/result.json')):
        trial = path.parent
        result = json.loads(path.read_text())
        trajectory = json.loads((trial / 'agent/trajectory.json').read_text())
        checks = json.loads((trial / 'verifier/checks.json').read_text())
        events = [json.loads(line) for line in
                  (trial / 'artifacts/app/output/benchmark-events.jsonl').read_text().splitlines()]
        calls = [(s['step_id'], c) for s in trajectory['steps']
                 for c in s.get('tool_calls', [])]
        mcp = [(step, c) for step, c in calls
               if c['function_name'].startswith('mcp-service_')]
        assert [(c['function_name'].removeprefix('mcp-service_'), c['arguments'])
                for _, c in mcp] == [(e['tool_name'], e['arguments']) for e in events]
        fault = next((i for i, e in enumerate(events) if e['is_error'] or
                      e.get('result', {}).get('value') == 'corrupted_stale_cache_record'), None)
        expected = events[0]['arguments']
        confirmed = next(i for i, e in enumerate(events)
                         if e['tool_name'] == 'read_record'
                         and e.get('result', {}).get('exists')
                         and e['result'].get('key') == expected['key']
                         and e['result'].get('value') == expected['value']
                         and (fault is None or i > fault))
        lag = mcp[confirmed][0] - mcp[fault][0] if fault is not None else None
        print(trial.name, checks['reward'], len(trajectory['steps']),
              len(calls), len(mcp), lag)
```

Bounded analysis brief (UTF-8 digest in provenance):

> Analyze all available mcp-recovery-v1 clean-twin, transient5xx, persistent-signature-error and silent-wrong-payload trials; trace observed errors, identical versus adapted retries, and step counts; distinguish harness, verifier and agent failures; do not run new model trials.

The embedded JSON is the machine-readable Stage-5 sidecar collection. Shared alternatives and discriminator are specified above; `confidence` is an analyst label, not a statistical probability. Source digests hash the currently promoted bytes unless explicitly named `result_task_checksum` or `lock_task_digest`. `analysis_provenance.cost_usd:null` means this session's cost was not measured; it is not a zero-cost claim. `review_status` remains pending human review.

```json
{
  "provenance": {
    "schema_version": 1,
    "analysis_id": "5ec5413e-d8bc-51e7-8414-6d6c6af9715b",
    "created_at": "2026-09-10",
    "review_status": "needs_human_review",
    "analysis_provenance": {
      "agent": "RecoveryAnalyst",
      "agent_version": null,
      "model": "devin/gpt-6-astra",
      "prompt_digest": "sha256:8bf7973c61100dab6d63a369d9104a0b354f2b59dfcf683d427ccfc8ab4db347",
      "prompt_digest_scope": "bounded analysis brief printed below, not the complete OMP system prompt",
      "rubric_digest": "sha256:a9a2f6a7763621408c6964f4bdeace731375b496f051e4386b53b879cc0e32b5",
      "cost_usd": null,
      "new_trial_runs": 0
    },
    "cohort_n": 14,
    "fault_trials_n": 7,
    "clean_trials_n": 7,
    "promoted_file_digests_checked": 163,
    "original_file_digests_checked": 42,
    "historical_worktree_verifier_digest": "sha256:2fb015f82ed32217947be3ff26df677df31d90ebb261f4cc3cedfdd600e621ee"
  },
  "analyses": [
    {
      "analysis_id": "f35fad57-09f3-59cb-a9d4-5e1901873e91",
      "source_trial_id": "c5b2d4b0-804d-46a4-82b2-3d62878decc2",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__2Li2u7z/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_d511d6fb5a30495cb1ae1d30",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-clean__2Li2u7z",
        "trial_id": "c5b2d4b0-804d-46a4-82b2-3d62878decc2",
        "path": "research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__2Li2u7z",
        "task_name": "recovery-clean",
        "task_id": "local-lab/mcp-rec-55410a4384115685",
        "lock_task_digest": "sha256:66bcaa79232b05c1fc757b647e014e10c6563c6ece07b6229f3234340eb5b821",
        "result_task_checksum": "edba680d3045281ab0ad71721a573501b1dbdb650a080804b1c52da53c625360",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23431,
        "output_tokens": 159,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:d3c4bd236b02de1ff892a56c4452e7e85c1f7ce16943f8cbc75fc3f53bf4c0e2",
          "trajectory": "sha256:c0173706eb24225b00b2577cb1dec93803df7887d1eb365b5638e227e079caed",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:0cf020630efa814cb4c7e25f1622444a58a4a3e6e7e1ea72a205c86d91bfd276",
          "lock": "sha256:644efabd6d4966e88f906348547a3798279be498392eda8f4a99cdd690d86d36"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "9fb52be5-e34a-5938-9fa2-31e32e7b90bb",
      "source_trial_id": "8430c4e7-3b40-440b-b556-b3cb80315b7e",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__Hj7RGKk/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_c6b276dfc16f49cdbee1f50f",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-clean__Hj7RGKk",
        "trial_id": "8430c4e7-3b40-440b-b556-b3cb80315b7e",
        "path": "research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__Hj7RGKk",
        "task_name": "recovery-clean",
        "task_id": "local-lab/mcp-rec-55410a4384115685",
        "lock_task_digest": "sha256:66bcaa79232b05c1fc757b647e014e10c6563c6ece07b6229f3234340eb5b821",
        "result_task_checksum": "edba680d3045281ab0ad71721a573501b1dbdb650a080804b1c52da53c625360",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23457,
        "output_tokens": 159,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:4c969ca88c9074bb5ef8fdfd24e1be4c54a90eb0bce5d69aa85a1a224e372d86",
          "trajectory": "sha256:e2cc76d016788fbc215a23d8e24fc668875f3c4003c830dbfadf3eef86a09307",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:5a1e11d10b13aef87e64b20099f702a2311deec60adccce98f4d09db927a0734",
          "lock": "sha256:644efabd6d4966e88f906348547a3798279be498392eda8f4a99cdd690d86d36"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "7975ffdd-d77b-5142-9572-0675bccc6a62",
      "source_trial_id": "7d7c98e5-649f-403e-81aa-dde73bb11516",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__PbWN3P6/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_db870018a6c1430687d75db8",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-clean__PbWN3P6",
        "trial_id": "7d7c98e5-649f-403e-81aa-dde73bb11516",
        "path": "research/evidence/runs/zai-flash-recovery-clean-twin-r3-amd64-verifier/recovery-clean__PbWN3P6",
        "task_name": "recovery-clean",
        "task_id": "local-lab/mcp-rec-55410a4384115685",
        "lock_task_digest": "sha256:66bcaa79232b05c1fc757b647e014e10c6563c6ece07b6229f3234340eb5b821",
        "result_task_checksum": "edba680d3045281ab0ad71721a573501b1dbdb650a080804b1c52da53c625360",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23489,
        "output_tokens": 203,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:6558a85460dbe371669f607e9f4a0d7ef2088dcc25bdebdf148440603eeee556",
          "trajectory": "sha256:7079004ab1c03e30ace99bff774af6fc139a6dc074669c33189685b4dde6c878",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:a182c01824046164585160253a51911893aadfe90193b66c999c1b970076964c",
          "lock": "sha256:644efabd6d4966e88f906348547a3798279be498392eda8f4a99cdd690d86d36"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "798a4e1f-e83a-5d01-9216-e8f274d99610",
      "source_trial_id": "125b05b4-f615-4b8f-bf57-a9d55296dbc0",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "verifier_false_negative",
      "category_scope": "candidate verifier scoring defect",
      "summary": "Candidate verifier false negative: error detected and roundtrip restored, but a diagnostic missing-key read closed the mutation window before fallback.",
      "confidence": "medium",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__HYxt7du/agent/trajectory.json",
          "step_id": 2,
          "tool_call_id": "call_d7ecb7b789a04a3195163971",
          "supports": "mcp-service_write_record"
        },
        {
          "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__HYxt7du/agent/trajectory.json",
          "step_id": 5,
          "tool_call_id": "call_a6f1aaa844f44baaa732b0a6",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery__HYxt7du",
        "trial_id": "125b05b4-f615-4b8f-bf57-a9d55296dbc0",
        "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__HYxt7du",
        "task_name": "recovery",
        "task_id": "local-lab/mcp-rec-8e76b61449e022d5",
        "lock_task_digest": "sha256:811138b4279f28964ab913f3773c9c71ecf9d1afea440bae7b9840adc140c2d5",
        "result_task_checksum": "19491fc7834aeb84ea59f6a959d4ef3d79a4f41c593a334b16b015bd20fa553b",
        "reward": 0.0,
        "fault_count": 1,
        "steps": 6,
        "llm_calls": 5,
        "tool_calls": 5,
        "mcp_calls": 5,
        "fault_step": 2,
        "confirmation_step": 5,
        "post_fault_steps_to_confirmation": 3,
        "post_fault_mcp_calls": 4,
        "identical_write_payload_on_retry": true,
        "causal_mutation": false,
        "auto_clear": true,
        "input_tokens": 40394,
        "output_tokens": 397,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:dd9be8492eed8d759f8d2ab441b87e800fe91b31e61d0577d3fed110625df3ec",
          "trajectory": "sha256:0301588f657e790be128e66b066444f9278f9fa99ab1d09b1352a57b5e339284",
          "checks": "sha256:9f4e7c36086d86b956372d58263f2b04010f36a6f8750053ac9ba661e35d3b67",
          "events": "sha256:8d566e1a2973cc3b421c857ed705857a303a86a40e886754f7678fd1da37687e",
          "lock": "sha256:8743928a5500d0bad3f2c3b6eb9260d32f3f3fc75277428ca94e71175b757c86"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "ede0d9c4-d3fd-5bc5-8bf3-77216d73228c",
      "source_trial_id": "c9474abc-42e7-4549-869a-7b8cd68d1a6c",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Observed recovery: 502 detected, auth refreshed, identical write retried and read confirmed. No agent failure established.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ari74b9/agent/trajectory.json",
          "step_id": 2,
          "tool_call_id": "call_593b04287b7444fc8ef80310",
          "supports": "mcp-service_write_record"
        },
        {
          "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ari74b9/agent/trajectory.json",
          "step_id": 5,
          "tool_call_id": "call_93c184ee340d45acbdf5e3a3",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery__ari74b9",
        "trial_id": "c9474abc-42e7-4549-869a-7b8cd68d1a6c",
        "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ari74b9",
        "task_name": "recovery",
        "task_id": "local-lab/mcp-rec-8e76b61449e022d5",
        "lock_task_digest": "sha256:811138b4279f28964ab913f3773c9c71ecf9d1afea440bae7b9840adc140c2d5",
        "result_task_checksum": "19491fc7834aeb84ea59f6a959d4ef3d79a4f41c593a334b16b015bd20fa553b",
        "reward": 1.0,
        "fault_count": 1,
        "steps": 6,
        "llm_calls": 5,
        "tool_calls": 4,
        "mcp_calls": 4,
        "fault_step": 2,
        "confirmation_step": 5,
        "post_fault_steps_to_confirmation": 3,
        "post_fault_mcp_calls": 3,
        "identical_write_payload_on_retry": true,
        "causal_mutation": true,
        "auto_clear": false,
        "input_tokens": 39598,
        "output_tokens": 253,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:515d17f5aecca3a94effd4de8c055f905b6f8c58487afc77f805eee628a48a86",
          "trajectory": "sha256:f0e2f69159ab1bc78a04f38ec4522be04c246b1c6805ce2c29a67f495558858b",
          "checks": "sha256:fd6a14e259828cec4ff0c0ff6cc5c30cc40a36aa1ccfd6e40842b9c55e3871d8",
          "events": "sha256:7d5ae913482f35d5f2aef8c9cbe36e06b54f870dc21075150f8ddddfb1821243",
          "lock": "sha256:8743928a5500d0bad3f2c3b6eb9260d32f3f3fc75277428ca94e71175b757c86"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "8c1a3955-20a0-5ebf-86ee-f387273bcb2d",
      "source_trial_id": "09f9d5c2-8372-4f9f-96a5-18d77d30a812",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Observed recovery: 502 detected, transport/schema diagnostics then auth refresh, identical write and matching read. Causal necessity of extra diagnostics not established.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ozALVY2/agent/trajectory.json",
          "step_id": 2,
          "tool_call_id": "call_a75565de4e0340a88afe8c22",
          "supports": "mcp-service_write_record"
        },
        {
          "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ozALVY2/agent/trajectory.json",
          "step_id": 7,
          "tool_call_id": "call_8a2cb0491e08416593fb90c5",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery__ozALVY2",
        "trial_id": "09f9d5c2-8372-4f9f-96a5-18d77d30a812",
        "path": "research/evidence/runs/zai-flash-recovery-transient5xx-p1-r3-amd64-verifier/recovery__ozALVY2",
        "task_name": "recovery",
        "task_id": "local-lab/mcp-rec-8e76b61449e022d5",
        "lock_task_digest": "sha256:811138b4279f28964ab913f3773c9c71ecf9d1afea440bae7b9840adc140c2d5",
        "result_task_checksum": "19491fc7834aeb84ea59f6a959d4ef3d79a4f41c593a334b16b015bd20fa553b",
        "reward": 1.0,
        "fault_count": 1,
        "steps": 8,
        "llm_calls": 7,
        "tool_calls": 7,
        "mcp_calls": 5,
        "fault_step": 2,
        "confirmation_step": 7,
        "post_fault_steps_to_confirmation": 5,
        "post_fault_mcp_calls": 4,
        "identical_write_payload_on_retry": true,
        "causal_mutation": true,
        "auto_clear": false,
        "input_tokens": 62373,
        "output_tokens": 814,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:7c91767e10dd6ff0bb8edaab2ab635865f5a7a9e05e9c39392d12f48c3dbaa41",
          "trajectory": "sha256:cf8b20ca9df19d11542459a7e54af0694d14d67432d7d8db550c7681879af76c",
          "checks": "sha256:fd6a14e259828cec4ff0c0ff6cc5c30cc40a36aa1ccfd6e40842b9c55e3871d8",
          "events": "sha256:1d18150872b64c5127dd9a16b4b81df576d3994d814777c71120f7e97accf857",
          "lock": "sha256:8743928a5500d0bad3f2c3b6eb9260d32f3f3fc75277428ca94e71175b757c86"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "0d45f9d5-fd16-5043-8e9d-d529ed3d064f",
      "source_trial_id": "2956cbe1-2eae-4d11-8316-af5b59dce2b6",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Observed recovery: 403 detected, auth refreshed, identical write retried and read confirmed.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__Lm9ohzb/agent/trajectory.json",
          "step_id": 2,
          "tool_call_id": "call_483e367b2999495996b9d6d7",
          "supports": "mcp-service_write_record"
        },
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__Lm9ohzb/agent/trajectory.json",
          "step_id": 5,
          "tool_call_id": "call_51ed0e77c90242f7865a82d8",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-persistent-signature-er__Lm9ohzb",
        "trial_id": "2956cbe1-2eae-4d11-8316-af5b59dce2b6",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__Lm9ohzb",
        "task_name": "recovery-persistent-signature-error-s42-fault",
        "task_id": "local-lab/mcp-rec-bb6774473ea0055b",
        "lock_task_digest": "sha256:8bb0473a525913b1843c8fc85ab486854a821550178f9ba3b8ad020f99e78ccd",
        "result_task_checksum": "ada15e5ec8232668e9c0fb2beeaae084eff6e5feac2f29441d58d8657ecc819c",
        "reward": 1.0,
        "fault_count": 1,
        "steps": 6,
        "llm_calls": 5,
        "tool_calls": 4,
        "mcp_calls": 4,
        "fault_step": 2,
        "confirmation_step": 5,
        "post_fault_steps_to_confirmation": 3,
        "post_fault_mcp_calls": 3,
        "identical_write_payload_on_retry": true,
        "causal_mutation": true,
        "auto_clear": false,
        "input_tokens": 39486,
        "output_tokens": 239,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:6b3e77da82f7dfaf80aa80bf28e723ae6b261a0e12fd90f7e4e36fbc3e48cde1",
          "trajectory": "sha256:b03d7eb3b46a71408e13762c0054ad1bfee69024e8a2045fbdcd2322b71081fe",
          "checks": "sha256:fd6a14e259828cec4ff0c0ff6cc5c30cc40a36aa1ccfd6e40842b9c55e3871d8",
          "events": "sha256:b5716bed48d2a348ebf1d2bde3721ac9620bf25be84cbdc09b09311ddd26b042",
          "lock": "sha256:a1f9731c7318eb1ca8af27e8b9968fa4b174739ef8c3756e06fc2fb8b6c43987"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "d67e2930-162a-504e-8e55-19700fa13d4f",
      "source_trial_id": "35e4ac24-cede-4188-8183-e0242a297413",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__QHaoyPA/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_6c4f6dd680b94c72b013647f",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-persistent-signature-er__QHaoyPA",
        "trial_id": "35e4ac24-cede-4188-8183-e0242a297413",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__QHaoyPA",
        "task_name": "recovery-persistent-signature-error-s1337-clean",
        "task_id": "local-lab/mcp-rec-83105fc9b1b20947",
        "lock_task_digest": "sha256:06d240ea6cbe818504476ac25f0b4cd502c6e6e6e02c27a3932d1d1d7f88d118",
        "result_task_checksum": "085c78b9f1c762e94aa03f120ef50d153ba663519ba8ae7256d193dedd1121e2",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23586,
        "output_tokens": 183,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:7cba2560b626f615d9461ed1f0480386bff8df3de667162562026fdb40f9a657",
          "trajectory": "sha256:50c3e12fa373a8365183fb5ed91f8e3395e44fa4853cd86cbcadd8d879aeacbb",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:d3787b6e7fcb07231739e3100d025e669ecce342da618da3c66a62681b661661",
          "lock": "sha256:b0536fd66d36002b24102f4297f99c16433ca88c2e6d52f469e651f19d2277c3"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "b04c220f-0ea7-5728-b4c2-2b4c6f657e3d",
      "source_trial_id": "564295df-5ec9-4b30-91c7-75ad6d2607cf",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Observed recovery: 403 detected, auth refreshed, identical write retried and read confirmed.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__YXTpcLZ/agent/trajectory.json",
          "step_id": 2,
          "tool_call_id": "call_f768224422b045eda96d6570",
          "supports": "mcp-service_write_record"
        },
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__YXTpcLZ/agent/trajectory.json",
          "step_id": 5,
          "tool_call_id": "call_ff88d94f57164df1b9185ed5",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-persistent-signature-er__YXTpcLZ",
        "trial_id": "564295df-5ec9-4b30-91c7-75ad6d2607cf",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__YXTpcLZ",
        "task_name": "recovery-persistent-signature-error-s1337-fault",
        "task_id": "local-lab/mcp-rec-f2d65c68031381e1",
        "lock_task_digest": "sha256:28eb9ecaefab3c4faa02154e18b5ddc30a830a79fa786ba87919af6cfa9c2d0f",
        "result_task_checksum": "6410ba5c4c2e10872a100ccc2538a97f86aed82e52bbb7d1a5c8d265d5bc4e43",
        "reward": 1.0,
        "fault_count": 1,
        "steps": 6,
        "llm_calls": 5,
        "tool_calls": 4,
        "mcp_calls": 4,
        "fault_step": 2,
        "confirmation_step": 5,
        "post_fault_steps_to_confirmation": 3,
        "post_fault_mcp_calls": 3,
        "identical_write_payload_on_retry": true,
        "causal_mutation": true,
        "auto_clear": false,
        "input_tokens": 39474,
        "output_tokens": 204,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:7ec1ca0828c0772ed466403d33b919ee5e9364c09cb240b6811b6e0b2543dbb4",
          "trajectory": "sha256:745c027e3fdbe19d36dc30675553f84733523fd199e4d4cd42d48a2472bb111e",
          "checks": "sha256:fd6a14e259828cec4ff0c0ff6cc5c30cc40a36aa1ccfd6e40842b9c55e3871d8",
          "events": "sha256:4d5720cbb3df255c8ba97a244563a6414d6ef1187743f9964a54862f62a5f455",
          "lock": "sha256:a9b863571b1ecb27249f3c1d540e950daba7fcc6ba75e9b80d20ca493888da47"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "0bc25297-ecd3-5402-84f6-ed2be79d5a62",
      "source_trial_id": "e8bc6a7e-bf43-41fb-9a37-e57f757cfa95",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__mp4YbvW/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_d37d90efe24848bca866c595",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-persistent-signature-er__mp4YbvW",
        "trial_id": "e8bc6a7e-bf43-41fb-9a37-e57f757cfa95",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-persistent-signature-er__mp4YbvW",
        "task_name": "recovery-persistent-signature-error-s42-clean",
        "task_id": "local-lab/mcp-rec-46a38fb8ef932c93",
        "lock_task_digest": "sha256:8b03364fb7085ad34dacd34d34f3f96f8060b9a9724b9b1939df906ae99d476d",
        "result_task_checksum": "8f53b4be3f384ed7bc4f6280420b428baac612a3a611d9ee012c58d7c3dd41e4",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23337,
        "output_tokens": 161,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:af1f6f43db04ae800873e34bb41b50767d0424ecdb9f0f01910d5b836155cb72",
          "trajectory": "sha256:64bec0c5e74d554c63645adec4b7cda8c312c73249d1e69d027179a4fca878a4",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:4728ba1f861031ab0302479b35ff379d2488031ac55126701b9a91a1fb397796",
          "lock": "sha256:11884b9c38655dbd7b0c4f780913bb63059df478c6bc11dba1dd9aecf9851c88"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "1fe6ec03-76b4-5992-9daa-3735cdb1b10a",
      "source_trial_id": "4763c5f7-3fd2-4b80-b707-1b28f23c9042",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__UBdmtRU/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_1f65759187aa4a7fa93257e5",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-silent-wrong-payload-s1__UBdmtRU",
        "trial_id": "4763c5f7-3fd2-4b80-b707-1b28f23c9042",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__UBdmtRU",
        "task_name": "recovery-silent-wrong-payload-s1337-clean",
        "task_id": "local-lab/mcp-rec-72e1c0ae6760a7e4",
        "lock_task_digest": "sha256:f4a68f15cfe2807ff356a9d455d9ab5fe557a6d7835fbecf5ec31d67c8dce007",
        "result_task_checksum": "32aeeb15ce434b798a6f9474f53738bf61f7236b4d363ad98b5af70684e829ac",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23392,
        "output_tokens": 133,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:d4d6a2b44420075a770c79316a1a527c38369c1f1d7b7db77358cbd33e1670bb",
          "trajectory": "sha256:eec8afab78516db2b588c8a6d4fc440be25c21751170bc1016888462d809b994",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:5ed622a8fcd497c546e995d98393fb677c56b7ef0973f2404234b6bfdbcb5edc",
          "lock": "sha256:006a51f484e05ed4f9b88a6f52126992895ab2b88b51dc0a338d3eb931a69aee"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "112d3ebb-afc0-5520-8616-b6a6c62089d4",
      "source_trial_id": "70bfd83e-2d8a-4571-b2ea-62aac2ac56f9",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Observed recovery: successful but corrupted read detected, fallback used, identical write and matching read.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__fZmgR5y/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_882d5d8848b14c1f938935ad",
          "supports": "mcp-service_read_record"
        },
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__fZmgR5y/agent/trajectory.json",
          "step_id": 6,
          "tool_call_id": "call_7a718d1d73984b00a6ca2c61",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-silent-wrong-payload-s1__fZmgR5y",
        "trial_id": "70bfd83e-2d8a-4571-b2ea-62aac2ac56f9",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s1__fZmgR5y",
        "task_name": "recovery-silent-wrong-payload-s1337-fault",
        "task_id": "local-lab/mcp-rec-44733fd9a42aaf99",
        "lock_task_digest": "sha256:ce9d0add9cb89996e9b63c19636989abc2d8a79bf8c785593b5bae983fa5e6f3",
        "result_task_checksum": "bcb7cbb71c0c57a94d6a6f4cc966ab93e0cc09197e3ac712a9dea8a85d485140",
        "reward": 1.0,
        "fault_count": 1,
        "steps": 7,
        "llm_calls": 6,
        "tool_calls": 5,
        "mcp_calls": 5,
        "fault_step": 3,
        "confirmation_step": 6,
        "post_fault_steps_to_confirmation": 3,
        "post_fault_mcp_calls": 3,
        "identical_write_payload_on_retry": true,
        "causal_mutation": true,
        "auto_clear": false,
        "input_tokens": 48314,
        "output_tokens": 319,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:340f9980ab87806ca023802892cbf59ffffd62e3d9b3e67d321de2281c01e8e8",
          "trajectory": "sha256:5bd00a824d965a78227507f5dcdb7d4d4349de92de764d83b255942c4ebee841",
          "checks": "sha256:eeae8c0b503a8a99e90cb971cf69c1be27ba7f0d66c297e3b25ec58c1aea5e7f",
          "events": "sha256:63aa13da5f534b1038a27f7e5a4881acc11e20edfff66154ec3fd78904930e39",
          "lock": "sha256:ce939a468430125514be9d895790ef50ddbb72e0148cda996fd8106fe6a422c5"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "5fd9c01f-3624-5aae-aa28-5cdfbcc52b4e",
      "source_trial_id": "2dd509cf-95fc-47ef-a99f-232b298a91db",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "unknown",
      "category_scope": "unknown means no failure mechanism established, not an unsuccessful trial",
      "summary": "Clean control: first write succeeded and independent read matched; no recovery opportunity occurred.",
      "confidence": "high",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__PmKJB8T/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_d31b1493c74f461d844fc9e3",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-silent-wrong-payload-s4__PmKJB8T",
        "trial_id": "2dd509cf-95fc-47ef-a99f-232b298a91db",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__PmKJB8T",
        "task_name": "recovery-silent-wrong-payload-s42-clean",
        "task_id": "local-lab/mcp-rec-5ed841f44283eeac",
        "lock_task_digest": "sha256:9a4a58ffd789501c01c5b102fe9e9618a0bc973cd8661af2dcd6fc9e0f85e937",
        "result_task_checksum": "8931c7393feb4f20f7ce03dbf0eeb9711524bdd124e6d6c446386bbfdd0f860c",
        "reward": 1.0,
        "fault_count": 0,
        "steps": 4,
        "llm_calls": 3,
        "tool_calls": 2,
        "mcp_calls": 2,
        "fault_step": null,
        "confirmation_step": 3,
        "post_fault_steps_to_confirmation": null,
        "post_fault_mcp_calls": null,
        "identical_write_payload_on_retry": null,
        "causal_mutation": null,
        "auto_clear": false,
        "input_tokens": 23636,
        "output_tokens": 172,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:b8c5f5c8b37995be69c8f0bdfe80d346a6ad02ac86d448faff4843799d81e7f8",
          "trajectory": "sha256:0db78bd3a9092813db853f16e40318f5fd0a1bf624eae9086363de10d39d4fe4",
          "checks": "sha256:6dd01d21786d82da4640fce62ce20d7d9c593f16825a12203e8cfaa71e2d4017",
          "events": "sha256:89341923ae7766e993d125bc6ed98bc69fab5ebfc5e49e6d5d3a669e91c78167",
          "lock": "sha256:a91c401d575c918453bc3c9d87a86737a925e223fc724e7b0823302774b6ac10"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    },
    {
      "analysis_id": "c07b7a82-aba9-5d1e-8678-6d304037915d",
      "source_trial_id": "d7829c6e-5e8d-4a92-9b95-c88eab6dee69",
      "validity": "valid_agent_attempt_with_provenance_limits",
      "primary_category": "evidence_use",
      "category_scope": "unsupported causal explanation, not task failure",
      "summary": "Successful recovery with unsupported final attribution to credentials: fallback preceded extra auth refresh; trajectory does not isolate causal repair.",
      "confidence": "medium",
      "evidence": [
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__cVZm97U/agent/trajectory.json",
          "step_id": 3,
          "tool_call_id": "call_55fcd403ed50417dbbf43444",
          "supports": "mcp-service_read_record"
        },
        {
          "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__cVZm97U/agent/trajectory.json",
          "step_id": 8,
          "tool_call_id": "call_5e1e21e71c394a91a370d8ba",
          "supports": "mcp-service_read_record"
        }
      ],
      "facts": {
        "trial": "recovery-silent-wrong-payload-s4__cVZm97U",
        "trial_id": "d7829c6e-5e8d-4a92-9b95-c88eab6dee69",
        "path": "research/evidence/runs/zai-wave2-flash-matrix/recovery-silent-wrong-payload-s4__cVZm97U",
        "task_name": "recovery-silent-wrong-payload-s42-fault",
        "task_id": "local-lab/mcp-rec-d0311eb59a500eeb",
        "lock_task_digest": "sha256:faa69e1263f65ba2fc981c8de22416a02a97d8ccca7780b89ad4a37e865438f6",
        "result_task_checksum": "5ae92065bcadd0df4f4ed576cbdcaeed185661764fbc4f35da2199b651bf8405",
        "reward": 1.0,
        "fault_count": 1,
        "steps": 9,
        "llm_calls": 8,
        "tool_calls": 7,
        "mcp_calls": 7,
        "fault_step": 3,
        "confirmation_step": 8,
        "post_fault_steps_to_confirmation": 5,
        "post_fault_mcp_calls": 5,
        "identical_write_payload_on_retry": true,
        "causal_mutation": true,
        "auto_clear": false,
        "input_tokens": 65517,
        "output_tokens": 392,
        "cost_usd": null,
        "source_digests": {
          "result": "sha256:14d846d51b4732dfd984f4c278014a0ba1e3376f31861c838c20e22024fafd0f",
          "trajectory": "sha256:b43df949cc6e926fe8ff667ae4f1d278290622f35bc32da1b1dcaf682709b618",
          "checks": "sha256:eeae8c0b503a8a99e90cb971cf69c1be27ba7f0d66c297e3b25ec58c1aea5e7f",
          "events": "sha256:f9cd283771d5621d21117d9bad727882df32544e2680879434b48222c46ea5b8",
          "lock": "sha256:5cb897f5423298957fc8d12ca37c00e5651a466219e549cf2243d35c6c2b5a86"
        },
        "prompt_digest": "sha256:ee28aee8908b95c9cdac07805926f1eda501d7edb15233a2529b1260c9f54319"
      }
    }
  ]
}
```
