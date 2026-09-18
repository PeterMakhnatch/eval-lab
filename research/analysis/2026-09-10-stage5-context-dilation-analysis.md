---
status: needs-human-review
type: stage5-analysis
created: 2026-09-10
scope: context-dilation
source: promoted Harbor evidence
---

# Stage 5 — Context dilation, retrieval fidelity, and latest-value retention

## Answer

**64k semantic distractors reduce exact-protocol success in the baseline sample, but do not demonstrate needle/value decay.** Phase-A semantic pass rates are **2/6 (33.3%) → 4/6 (66.7%) → 1/6 (16.7%)** at 4k, 16k, and 64k. Neutral rates are **5/6 (83.3%) → 5/6 (83.3%) → 2/6 (33.3%)**. The semantic curve is not monotonic; the semantic–neutral gap is largest at 4k, not 64k.

**Unit correction:** these task labels denote **4,096 / 16,384 / 65,536 bytes**, not measured tokenizer tokens. The requested 4k/16k/64k-token experiment does not exist in this evidence slice. The historical names are retained below but must not be interpreted as context-window token lengths. `library/benchmarks/action-memory-v1/dose_ladder.py` declares `DOSE_LADDER_BYTES`, and both original experiment reports explicitly specify byte doses. Cumulative prompt-token counts below are a different measure, not peak context occupancy.

Across **114 non-timeout attempts**, the final mutation matches the entity, attribute, and latest token in the retrieved critical override record in **114/114**. All **52 scored failures** fail the retrieval-path predicate, rather than exhibiting a wrong final binding. At 64k, new observable problems include **unlisted-handle `not_found`, duplicated retrieval, incomplete coverage, and premature mutation**. Pure ordering failures already occur at 4k. These are retrieval/execution reliability findings, not proof that the needle was forgotten.

## 1. Cohort and source authority

The requested prefixes are absent from the current `runs/` root. Their five canonical promoted copies are under `research/evidence/runs/`; no source run was edited. Selection includes every immediate trial `result.json` in these five jobs, rather than job rollups, hidden retries, or other action-memory campaigns.

| Alias | Exact promoted job | Attempts | Scored | Passes | Infrastructure exclusions |
|---|---|---:|---:|---:|---:|
| Phase-A | `zai-overnight-action-phase-a-v2-20260830` | 36 | 36 | 19 | 0 |
| E0b | `zai-e0b-handle-representation-r2-20260830` | 72 | 72 | 42 | 0 |
| Repeats | `zai-wave2-action64k-s1337-repeats` | 4 | 4 | 0 | 0 |
| Sequential-default | `zai-wave2-action64k-s1337-sequential-scaffold` | 2 | 0 | — | 2 |
| Sequential-t3 | `zai-wave2-action64k-s1337-sequential-scaffold-t3` | 2 | 2 | 1 | 0 |
| **Inventory total, not a pooled efficacy estimate** | | **116** | **114** | **62** | **2** |

All 116 results identify GLM-5.3-Flash, OpenCode 1.18.25 via `evallab.harbor_zai_opencode:ZaiOpenCodeAgent`, Docker, and Harbor 0.21.0. Phase-A and E0b use seeds 42, 1337, 2026 with two executions per deterministic cell; six attempts per dose/noise/representation represent only **three independent seeds**. Repeats and scaffold jobs use seed 1337. E0b has 4k and 16k only, with `opaque`, `indexed`, and `range_batch` representations; its 64k cells are **unobserved**, not zero-success cells.

Keep these cohorts separate: E0b changes representation and task identity; the sequential intervention changes instructions; t3 additionally changes the agent timeout multiplier. The byte-dose axis itself increases the mandatory read count (17 → 65 → 257), so dose and retrieval burden are inseparable here. Original Phase-A/E0b reports classify this as Darwin/public-egress calibration, not Linux/proxy-isolated causal-grade evidence. No cross-model claim is licensed.

### Evidence validity and limitations

- Parsed 116 trial results, ATIF-v1.7 trajectories, verifier result/reward pairs, and benchmark-event logs. All trial and job configurations/locks are present.
- Verified **1,175 promoted-file SHA-256 entries** against the five `PROMOTION.json` manifests: **zero mismatches**. No required extraction input is missing. Trial reward, verifier JSON reward, and `reward.txt` agree in **116/116** cases.
- Per-trial ATIF step IDs are unique. This is structural extraction, **not** a claim of running a complete external ATIF schema validator.
- Two default-scaffold results contain `AgentTimeoutError`, `missing_final_state_evidence`, and stored reward **0.0**. Earlier prose calls these unscored/null; the current promoted artifacts store zero. This report preserves the stored value but **excludes both from capability pass-rate denominators**. Neither reached mutation.
- Runtime task paths recorded in results are absent for **116/116** trials. Task digests are preserved in locks/results, but original task/verifier snapshots, hidden truth, image digests, full model settings, and matching oracle/NOP controls were not independently validated here. Current verifier source explains the predicate but is not asserted byte-identical to every historical verifier.
- User/system prompt text is promotion-redacted; prompt hashes and lengths remain. Raw agent logs/runtime state are intentionally omitted. No claim depends on reconstructing that omitted material.
- Direct ATIF versus server retrieval sequences agree for **114/116** trials after batch expansion. Two Phase-A semantic-64k exceptions (`6vDNEHZ`, `8aYeUds`) show only 7 and 4 direct ATIF chunk calls versus 264 and 522 server reads. Server events are authoritative for retrieval counts; ATIF is authoritative only for its recorded steps/calls.
- These are direct canonical-artifact statistics, not rates over unfiltered feature-table rows. No Parquet/Postgres feature status is presumed. Full validity certification and verifier false-positive/negative adjudication remain unavailable; observed scored attempts are retained with these provenance limits.

## 2. Comparative degradation curves

A pass means no trial exception and verifier reward 1.0. Steps mean `len(trajectory.steps)`, including the initial user step; every trajectory here has exactly one user step, so recorded LLM-call means are one lower. A step can contain many calls. `range_batch` is one physical call but is expanded into logical reads for coverage/order; server reads are not interchangeable with ATIF steps.

### Phase-A: the complete three-dose ladder

| Dose label (bytes) | Noise | Pass / scored | Pass rate | ATIF steps mean | Median [min–max] | LLM calls mean | Cumulative prompt tokens mean |
|---|---|---:|---:|---:|---:|---:|---:|
| 4k | neutral_padding | 5/6 | 83.3% | 5.00 | 5 [5–5] | 4.00 | 35,073 |
| 4k | semantic_distractor | 2/6 | 33.3% | 5.00 | 5 [5–5] | 4.00 | 36,392 |
| 16k | neutral_padding | 5/6 | 83.3% | 6.17 | 6.5 [5–7] | 5.17 | 59,946 |
| 16k | semantic_distractor | 4/6 | 66.7% | 6.67 | 7 [5–8] | 5.67 | 73,467 |
| 64k | neutral_padding | 2/6 | 33.3% | 17.00 | 17 [14–21] | 16.00 | 406,318 |
| 64k | semantic_distractor | 1/6 | 16.7% | 14.83 | 14.5 [13–18] | 13.83 | 405,164 |

```text
Byte-dose axis                    4k          16k          64k
Neutral protocol pass             83.3%  ───  83.3%  ───  33.3%
Semantic protocol pass            33.3%  ───  66.7%  ───  16.7%
Neutral mean ATIF steps             5.00 ───   6.17 ───  17.00
Semantic mean ATIF steps            5.00 ───   6.67 ───  14.83
Neutral correct latest binding     6/6   ───   6/6   ───   6/6
Semantic correct latest binding    6/6   ───   6/6   ───   6/6
```

**Observed degradation:** 16k→64k protocol success falls **50 percentage points in both arms**. From 4k→64k it falls 50 points neutral and 16.7 points semantic. Mean steps increase 3.40× neutral and 2.97× semantic; cumulative prompt tokens increase approximately 11.6× and 11.1× respectively. At 64k semantic trails neutral by one pass in six attempts (−16.7 points), not evidence of a uniquely accelerating semantic-distractor effect. The three-seed repeated sample does not support a population curve or precise causal effect estimate.

### E0b: representation-specific curves (no 64k observations)

Each cell is six attempts. Every attempt retains the correct latest binding; differences below concern the exact retrieval protocol.

| Representation | Noise | 4k pass | 16k pass | 64k | 4k steps mean [range] | 16k steps mean [range] |
|---|---|---:|---:|---|---:|---:|
| opaque | neutral_padding | 5/6 (83.3%) | 5/6 (83.3%) | Not run | 5.00 [5–5] | 6.83 [5–9] |
| opaque | semantic_distractor | 5/6 (83.3%) | 3/6 (50.0%) | Not run | 5.00 [5–5] | 6.17 [5–8] |
| indexed | neutral_padding | 3/6 (50.0%) | 2/6 (33.3%) | Not run | 5.00 [5–5] | 6.50 [5–8] |
| indexed | semantic_distractor | 0/6 (0.0%) | 1/6 (16.7%) | Not run | 5.00 [5–5] | 7.50 [6–8] |
| range_batch | neutral_padding | 5/6 (83.3%) | 6/6 (100.0%) | Not run | 5.17 [5–6] | 5.00 [5–5] |
| range_batch | semantic_distractor | 4/6 (66.7%) | 3/6 (50.0%) | Not run | 5.33 [5–6] | 5.50 [5–6] |

The `indexed` intervention is not a repair: 6/24 passes versus 18/24 opaque. `range_batch` also passes 18/24 while using fewer physical calls; equal totals do not establish equivalence. All E0b trials cover the complete required read set. Its 30 failures comprise 24 pure reorderings and six duplicate-read cases; these are exclusive mechanical subtypes, not six additional failures. Do not splice its 4k/16k values onto Phase-A's 64k point.

### Wave2 64k repeats and sequential intervention

| Cohort | Noise | Pass / scored | ATIF steps (individual) | Logical reads (individual) | Comment |
|---|---|---:|---|---|---|
| Repeats | neutral_padding | 0/2 | 18, 11 | 257, 257 | Each misses one expected handle through invalid substitution |
| Repeats | semantic_distractor | 0/2 | 16, 18 | 257, 259 | Invalid handles; one attempt also duplicates reads |
| Sequential-default | neutral_padding | — / 0 | 90 | 88 | Timeout; excluded |
| Sequential-default | semantic_distractor | — / 0 | 86 | 84 | Timeout; excluded; only 83 unique expected handles |
| Sequential-t3 | neutral_padding | 1/1 | 261 | 257 | Exact ordered retrieval and correct mutation |
| Sequential-t3 | semantic_distractor | 0/1 | 236 | 232 | Mutates 25 reads early; correct latest binding |

The t3 neutral/semantic trials consume **6,683,558 / 7,454,261 cumulative prompt tokens**, totaling **14,137,819**. The semantic failure is not a stale-value mistake: ATIF step **235**, call `call_62c0f6db5a5847929f5bee29`, binds the correct `f3e822e6_v2` value. It omits the last 25 required reads. One paired seed cannot establish a general scaffold effect, and its timeout/cost overhead must not disappear into a pooled 64k average.

## 3. What appears at 64k that does not appear at 4k?

Primary comparison below is Phase-A, preserving the same cohort. Counts are per trial; fault rows overlap.

| Observable condition | 4k (n=12) | 16k (n=12) | 64k (n=12) |
|---|---:|---:|---:|
| Protocol failures | 5 | 3 | 9 |
| Application-level `not_found` / unlisted handle | 0 | 0 | 6 |
| At least one duplicate read | 0 | 0 | 5 |
| Missing at least one expected handle | 0 | 0 | 4 |
| Complete coverage but wrong protocol order/multiplicity | 5 | 3 | 5 |
| Incorrect final latest-value binding | 0 | 0 | 0 |

1. **Opaque-handle transcription/substitution:** neutral `action-64k-neutral_padding-s1337__FFSbF3C` sends `ctx_2110473c018845ab0cc32bf6` at server event **85** (84th read), receiving `value.error = not_found`. ATIF step **7**, call `call_8d920d813b864111a8710d4b`, preserves the request. It covers 256/257 expected handles but subsequently binds the correct value at step 17. Importantly, the event envelope says `status: ok`, `event_type: tool_call_success`, `is_error: false`; looking only at transport-level error flags would miss this failure.
2. **Repeated/replayed retrieval:** semantic `action-64k-semantic_distractor-s__8aYeUds` records **522 server reads, 264 duplicates, one invalid handle**, despite complete expected-handle coverage. The first protocol divergence is read 5, earlier than its invalid-handle event 89. Direct ATIF exposes only four single-chunk calls, so server replay/alternate execution and capture incompleteness remain plausible. Do not assert 522 LLM steps or attribute every duplicate to independent model calls.
3. **Premature completion under the sequential scaffold:** semantic `action-64k-semantic_distractor-s__A67eDZ2` stops at 232/257 reads and mutates. This is unique to this 64k intervention slice, not a clean dose-only contrast.
4. **Timeout before completing the retrieval chain:** default scaffold trials stop at 88 and 84 reads; these are separate harness-budget outcomes, not semantic needle errors. Their 4k counterparts were not run.

**Counterexamples:** Phase-A has three 64k passes (two neutral, one semantic). The semantic success `action-64k-semantic_distractor-s__eFWYpcQ` performs 257 exact reads in 13 ATIF steps. The sequential neutral t3 pass takes 261 steps. Many more steps are therefore neither necessary nor sufficient for success.

## 4. Stage 5 interpretation and verifier audit

**Earliest supported failure:** the first server read differing from the listed sequence, or the first premature mutation after a shortened sequence. An invalid handle can be later than an earlier ordering divergence; Appendix B preserves that distinction. At 4k, failures can arise within a single parallel-call step even though every record and the right token were retrieved.

**Primary taxonomy:** `tool_use` for observable sequencing, argument, duplication, and premature-mutation failures; `harness_failure` for the two timeout exclusions. `context_management` is a possible causal interpretation, not established by the dose label. `planning`, execution scheduling, alternate tool paths, and adapter capture are alternatives. A successful final binding is not enough to pass this benchmark's broader contract.

**Verifier behavior:** the current verifier checks exact retrieval order/coverage and that one mutation comes last **before** comparing final entity/attribute/value. Stored results agree with that observable contract. A server `execute_mutation` status of `executed` is not the reward; it can coexist with a failed retrieval protocol. No accepted invalid final binding is observed. Conversely, a correct final binding plus out-of-order complete retrieval is an intended protocol rejection under this contract, not by itself a verifier false negative. Whether ordering belongs in a benchmark intended to measure memory is a **construct-validity question**, not a reason to silently regrade scores.

**Needle-decay conclusion:** correct binding is independently reconstructed from retrieved critical override content and compared to final mutation, not inferred from reward. This check succeeds for every non-timeout attempt, including all semantic 64k failures. This does not show perfect memory in general: the needle may be easy, instructions may permit direct copying, and hidden truth/control artifacts are unavailable. It does show that these failures should not be marketed as demonstrated loss of the latest-value needle.

Exclusive descriptive outcome partition across all selected jobs: 62 passes; 33 complete unique reorderings; 10 invalid-handle failures (may also duplicate/omit); eight duplicate-read failures without invalid handles; one incomplete-retrieval failure without those other faults; two timeouts. This is an inventory partition, not a pooled capability rate.

## 5. Smallest discriminating follow-up — proposal only

**Hypothesis:** the measured 64k deficit is dominated by retrieval orchestration rather than loss of the latest binding.

- Hold one existing 64k task/seed, byte dose, noise arm, model, adapter, tool schema, verifier, and timeout fixed. Change **only serial versus concurrent retrieval scheduling** using the same ordered handle list. Record both dispatch and server completion order.
- Compare final entity/attribute/value correctness separately from the unchanged strict protocol reward. Preserve application-level errors, physical calls, expanded logical reads, server event order, and ATIF correspondence.
- Under orchestration failure, serial dispatch improves protocol adherence while final binding remains correct. Under true needle loss, incorrect final binding appears even with complete ordered retrieval. Under verifier construct mismatch, value success persists despite protocol rejection.
- Before a billed comparison, recover/version-pin the actual task/verifier snapshots, establish matched oracle/NOP controls, and include an intentionally reordered complete-read negative control. Do not reuse old rewards as though a revised verifier had produced them.
- Proposed canary: one matched pair, one attempt per condition, concurrency 1, equal predeclared timeout. Do not combine neutral/semantic arm changes with scheduling changes in the same causal contrast. A later independent-seed replication is necessary before generalization.
- **Not authorized/executed.** A dollar ceiling cannot be derived from `cost_usd: null`; Peter must approve a hard spend/token/timeout ceiling and any paid execution. Stop on auth/infrastructure failure or budget breach. This report starts no new trials, models, cloud jobs, or ingestion writes.

## 6. Provenance and verification receipt

```json
{
  "schema_version": 1,
  "analysis_id": "91672cc2-2853-4d4d-ac91-577ab73bdda0",
  "analysis_provenance": {
    "agent": "ContextDilationAnalyst",
    "agent_version": "not exposed by harness",
    "model": "devin/gpt-6-astra",
    "prompt_digest": "sha256:150a38dce616edf1e24dbf4415044c192d2f87fb0d4d47dc70b4d576cacf926a",
    "prompt_text": "Analyze all promoted jobs with prefixes zai-overnight-action-phase-a-v2, zai-e0b-handle-representation-r2, and zai-wave2-action64k. Compare 4k/16k/64k neutral_padding versus semantic_distractor pass rates and ATIF steps; identify whether latest-value binding decays and which errors emerge at 64k. Separate infrastructure outcomes and interventions; cite source evidence and state limitations.",
    "rubric_digest": "sha256:a9a2f6a7763621408c6964f4bdeace731375b496f051e4386b53b879cc0e32b5",
    "created_at": "2026-09-10T21:52:01.825538+00:00",
    "cost_usd": null,
    "execution": "Read-only local Python extraction; no new model trial or judge invoked."
  },
  "source_corpus_digest": "sha256:b716724ba80795550ca2a06eae365742f097a07f1fd03f11d232abe4ac21c1f3",
  "source_corpus_digest_rule": "SHA256 of UTF-8 JSON (sort_keys=True, separators=(comma,colon)) for 696 path/sha256 objects sorted by repository-relative path; six files per trial: result.json, agent/trajectory.json, lock.json, config.json, verifier/result.json, artifacts/app/output/benchmark-events.jsonl",
  "review_status": "needs-human-review",
  "trials_analyzed": 116,
  "unavailable_mechanical_trial_analyses": 0,
  "detailed_representative_sidecars": 6,
  "scored_attempts": 114,
  "harness_exclusions": 2,
  "integrity_validation": {
    "manifest_files_checked": 1175,
    "manifest_mismatches": [],
    "required_missing": [],
    "reward_disagreements": []
  }
}
```

Evidence paths in the appendices are relative to the repository root. Source task digests are **recorded** hashes, not reconstructed runtime packages. Original design/report references:

- [Analysis rubric](../../docs/analysis-loop.md)
- [Phase-A experiment design](../evidence/zai-opencode-action-memory-phase-a-2026-08-30.md)
- [E0b experiment design](../evidence/zai-opencode-e0b-handle-representation-2026-08-30.md)
- [Sequential scaffold](../evidence/scaffolds/action-memory-sequential-retrieval-v1.md)
- [Dose generator](../../library/benchmarks/action-memory-v1/dose_ladder.py)
- [Current verifier](../../library/benchmarks/action-memory-v1/verifier.py)

## Appendix A — Machine-readable cell comparison

This CSV is the deterministic Stage 4 output computed before interpretation. Empty pass rate means no scored attempt, not 0%. Dose is bytes. Steps include timeout trajectories for their separate infrastructure rows.

```csv
job,representation,dose_bytes,noise,attempts,scored,passed,pass_rate,mean_steps,median_steps,min_steps,max_steps,mean_llm_calls,mean_tool_calls,mean_prompt_tokens,latest_binding_matches
zai-e0b-handle-representation-r2-20260830,indexed,4096,neutral_padding,6,6,3,0.5,5,5.0,5,5,4,19,33249.333333333336,6
zai-e0b-handle-representation-r2-20260830,indexed,4096,semantic_distractor,6,6,0,0.0,5,5.0,5,5,4,19,34546.166666666664,6
zai-e0b-handle-representation-r2-20260830,indexed,16384,neutral_padding,6,6,2,0.3333333333333333,6.5,6.0,5,8,5.5,67,54699.666666666664,6
zai-e0b-handle-representation-r2-20260830,indexed,16384,semantic_distractor,6,6,1,0.16666666666666666,7.5,8.0,6,8,6.5,67,73452.33333333333,6
zai-e0b-handle-representation-r2-20260830,opaque,4096,neutral_padding,6,6,5,0.8333333333333334,5,5.0,5,5,4,19,35017.333333333336,6
zai-e0b-handle-representation-r2-20260830,opaque,4096,semantic_distractor,6,6,5,0.8333333333333334,5,5.0,5,5,4,19,36295.333333333336,6
zai-e0b-handle-representation-r2-20260830,opaque,16384,neutral_padding,6,6,5,0.8333333333333334,6.833333333333333,6.5,5,9,5.833333333333333,67,69595.5,6
zai-e0b-handle-representation-r2-20260830,opaque,16384,semantic_distractor,6,6,3,0.5,6.166666666666667,6.0,5,8,5.166666666666667,67,66684.16666666667,6
zai-e0b-handle-representation-r2-20260830,range_batch,4096,neutral_padding,6,6,5,0.8333333333333334,5.166666666666667,5.0,5,6,4.166666666666667,3.3333333333333335,34477.5,6
zai-e0b-handle-representation-r2-20260830,range_batch,4096,semantic_distractor,6,6,4,0.6666666666666666,5.333333333333333,5.0,5,6,4.333333333333333,3.3333333333333335,37502.333333333336,6
zai-e0b-handle-representation-r2-20260830,range_batch,16384,neutral_padding,6,6,6,1.0,5,5.0,5,5,4,3,36553.666666666664,6
zai-e0b-handle-representation-r2-20260830,range_batch,16384,semantic_distractor,6,6,3,0.5,5.5,5.5,5,6,4.5,3.5,49005.666666666664,6
zai-overnight-action-phase-a-v2-20260830,baseline,4096,neutral_padding,6,6,5,0.8333333333333334,5,5.0,5,5,4,19,35073.333333333336,6
zai-overnight-action-phase-a-v2-20260830,baseline,4096,semantic_distractor,6,6,2,0.3333333333333333,5,5.0,5,5,4,19,36392.166666666664,6
zai-overnight-action-phase-a-v2-20260830,baseline,16384,neutral_padding,6,6,5,0.8333333333333334,6.166666666666667,6.5,5,7,5.166666666666667,67,59946.333333333336,6
zai-overnight-action-phase-a-v2-20260830,baseline,16384,semantic_distractor,6,6,4,0.6666666666666666,6.666666666666667,7.0,5,8,5.666666666666667,67.33333333333333,73466.83333333333,6
zai-overnight-action-phase-a-v2-20260830,baseline,65536,neutral_padding,6,6,2,0.3333333333333333,17,17.0,14,21,16,259.6666666666667,406317.6666666667,6
zai-overnight-action-phase-a-v2-20260830,baseline,65536,semantic_distractor,6,6,1,0.16666666666666666,14.833333333333334,14.5,13,18,13.833333333333334,178.66666666666666,405164,6
zai-wave2-action64k-s1337-repeats,baseline,65536,neutral_padding,2,2,0,0.0,14.5,14.5,11,18,13.5,259,311090,2
zai-wave2-action64k-s1337-repeats,baseline,65536,semantic_distractor,2,2,0,0.0,17,17.0,16,18,16,260,510088,2
zai-wave2-action64k-s1337-sequential-scaffold,baseline,65536,neutral_padding,1,0,0,,90,90,90,90,89,89,1481752,0
zai-wave2-action64k-s1337-sequential-scaffold,baseline,65536,semantic_distractor,1,0,0,,86,86,86,86,85,85,1723357,0
zai-wave2-action64k-s1337-sequential-scaffold-t3,baseline,65536,neutral_padding,1,1,1,1.0,261,261,261,261,260,259,6683558,1
zai-wave2-action64k-s1337-sequential-scaffold-t3,baseline,65536,semantic_distractor,1,1,0,0.0,236,236,236,236,235,234,7454261,1
```
## Appendix B — Representative structured Stage 5 sidecars

`analysis_provenance_ref` resolves to §6. `unknown` on the passing counterexample means no failure category applies.

```json
[
  {
    "schema_version": 1,
    "analysis_id": "438ba437-da62-5242-b32f-0cc40d7adc03",
    "source_trial_id": "85621347-f72a-4fc3-bcfc-e246c90d2aa0",
    "source_digests": {
      "result": "sha256:9ba4b1f94e0c394a8427f02c4c3c0e6d637d85b0b7281a6c3899604b0134f41e",
      "trajectory": "sha256:e3a569ef919d0e67fcb76a1e9f3a492311024d570dc1bf0c9954e885fcb69f06",
      "task": "sha256:52f5d8daa9670402b58c4add381fc822064ee3b276a5972d6a2d25c299f6ffe8",
      "task_digest_status": "Recorded lock digest; original runtime task absent, not independently rehashed"
    },
    "analysis_provenance_ref": "report.analysis_provenance",
    "validity": "valid_agent_attempt_with_provenance_limits",
    "primary_category": "tool_use",
    "summary": "Complete coverage but out-of-order retrieval at 4k; mutation still uses the current binding.",
    "evidence": [
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-4k-semantic_distractor-s1__V9ZDDrL/verifier/result.json",
        "supports": "incomplete_or_reordered_context_retrieval"
      },
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-4k-semantic_distractor-s1__V9ZDDrL/artifacts/app/output/benchmark-events.jsonl",
        "first_divergent_read_ordinal": 2,
        "supports": "17/17 unique expected handles; 17 logical reads; 0 duplicates; 0 application-level not_found"
      },
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-4k-semantic_distractor-s1__V9ZDDrL/agent/trajectory.json",
        "step_id": 4,
        "tool_call_id": "call_52a4759b20aa4ed1bdaeafcb",
        "supports": "Mutation exactly matches entity/attribute/latest token in retrieved CRITICAL STATE INVERSION record."
      }
    ],
    "alternative_explanations": [
      "Parallel call scheduling versus model sequencing remains unresolved."
    ],
    "proposed_discriminator": "Use the fixed matched task with serial versus concurrent retrieval and retain server plus ATIF ordering; independently score final binding and retrieval protocol. Requires approval before execution.",
    "confidence": "high for observable mechanics; low for causal attribution"
  },
  {
    "schema_version": 1,
    "analysis_id": "ce45844b-0815-566e-8fb6-c5083249289b",
    "source_trial_id": "88a42501-a147-4b6f-b604-8cfd178aaaf6",
    "source_digests": {
      "result": "sha256:1031243027faff0423f8c6cc083d5c19b84de0702b47acdba1d3845f33431530",
      "trajectory": "sha256:4e01330da44443f47cb9a82034d1af046f4de4b968b080a725f6e054708e05d7",
      "task": "sha256:ea0df9fd45804932b08083202eef5606ad8b7bd86dcf5fc8c2159afac1581cfb",
      "task_digest_status": "Recorded lock digest; original runtime task absent, not independently rehashed"
    },
    "analysis_provenance_ref": "report.analysis_provenance",
    "validity": "valid_agent_attempt_with_provenance_limits",
    "primary_category": "tool_use",
    "summary": "The 84th read substitutes an unlisted handle; application-level not_found at event 85, despite outer status ok. Final mutation is correct.",
    "evidence": [
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-64k-neutral_padding-s1337__FFSbF3C/verifier/result.json",
        "supports": "incomplete_or_reordered_context_retrieval"
      },
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-64k-neutral_padding-s1337__FFSbF3C/artifacts/app/output/benchmark-events.jsonl",
        "first_divergent_read_ordinal": 84,
        "supports": "256/257 unique expected handles; 257 logical reads; 0 duplicates; 1 application-level not_found"
      },
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-64k-neutral_padding-s1337__FFSbF3C/agent/trajectory.json",
        "step_id": 17,
        "tool_call_id": "call_d60a5eb8fc7348c6901a5f2c",
        "supports": "Mutation exactly matches entity/attribute/latest token in retrieved CRITICAL STATE INVERSION record."
      }
    ],
    "alternative_explanations": [
      "Opaque-handle transcription failure, not evidence of forgotten latest value."
    ],
    "proposed_discriminator": "Use the fixed matched task with serial versus concurrent retrieval and retain server plus ATIF ordering; independently score final binding and retrieval protocol. Requires approval before execution.",
    "confidence": "high for observable mechanics; low for causal attribution"
  },
  {
    "schema_version": 1,
    "analysis_id": "dc32e986-11ad-58f2-9d83-c176d40dcaa2",
    "source_trial_id": "c115f5a4-3078-4b83-9948-3ed1b7b3b9a2",
    "source_digests": {
      "result": "sha256:80e3a856c3b5a4c3ac03aa37db210f447a75e800a9314a803daa19a869149ff7",
      "trajectory": "sha256:1b1a2352ab4d58de8b512d0938b7131f1c050954b81a3617ef2a1cd5e4a77390",
      "task": "sha256:ed5ee146b141d4337dfef8515c8d1e1e748e54f1819f6564faba01d353ac4b90",
      "task_digest_status": "Recorded lock digest; original runtime task absent, not independently rehashed"
    },
    "analysis_provenance_ref": "report.analysis_provenance",
    "validity": "valid_agent_attempt_with_provenance_limits",
    "primary_category": "tool_use",
    "summary": "522 server reads include 264 duplicates and an invalid handle; retrieval diverges at read 5. ATIF exposes only 4 direct chunk calls. Final mutation is correct.",
    "evidence": [
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-64k-semantic_distractor-s__8aYeUds/verifier/result.json",
        "supports": "incomplete_or_reordered_context_retrieval"
      },
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-64k-semantic_distractor-s__8aYeUds/artifacts/app/output/benchmark-events.jsonl",
        "first_divergent_read_ordinal": 5,
        "supports": "257/257 unique expected handles; 522 logical reads; 264 duplicates; 1 application-level not_found"
      },
      {
        "path": "research/evidence/runs/zai-overnight-action-phase-a-v2-20260830/action-64k-semantic_distractor-s__8aYeUds/agent/trajectory.json",
        "step_id": 14,
        "tool_call_id": "call_cb542dc1386f4324a3799d7b",
        "supports": "Mutation exactly matches entity/attribute/latest token in retrieved CRITICAL STATE INVERSION record."
      }
    ],
    "alternative_explanations": [
      "Alternate execution/capture path means direct ATIF call counts understate server activity."
    ],
    "proposed_discriminator": "Use the fixed matched task with serial versus concurrent retrieval and retain server plus ATIF ordering; independently score final binding and retrieval protocol. Requires approval before execution.",
    "confidence": "high for observable mechanics; low for causal attribution"
  },
  {
    "schema_version": 1,
    "analysis_id": "cfa14ca4-7ee0-506a-8117-4320fe02bcf2",
    "source_trial_id": "a111e0cd-3e36-4bca-b48c-6db2ffe150b0",
    "source_digests": {
      "result": "sha256:2df4d168bdcb1876f10ceda5011f9abdb08bd9ed799ff826dc9ffb51b386c20b",
      "trajectory": "sha256:c65671b092f41992a839bce271cc28db6f52fd475f026880e075d775bfe8a90c",
      "task": "sha256:aa0beec3658fa27c7040de920eba595634bbf1561e98850580946a74956d9f72",
      "task_digest_status": "Recorded lock digest; original runtime task absent, not independently rehashed"
    },
    "analysis_provenance_ref": "report.analysis_provenance",
    "validity": "valid_agent_attempt_with_provenance_limits",
    "primary_category": "tool_use",
    "summary": "Sequential semantic run mutates after only 232 of 257 required reads; latest binding remains correct.",
    "evidence": [
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold-t3/action-64k-semantic_distractor-s__A67eDZ2/verifier/result.json",
        "supports": "incomplete_or_reordered_context_retrieval"
      },
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold-t3/action-64k-semantic_distractor-s__A67eDZ2/artifacts/app/output/benchmark-events.jsonl",
        "first_divergent_read_ordinal": 233,
        "supports": "232/257 unique expected handles; 232 logical reads; 0 duplicates; 0 application-level not_found"
      },
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold-t3/action-64k-semantic_distractor-s__A67eDZ2/agent/trajectory.json",
        "step_id": 235,
        "tool_call_id": "call_62c0f6db5a5847929f5bee29",
        "supports": "Mutation exactly matches entity/attribute/latest token in retrieved CRITICAL STATE INVERSION record."
      }
    ],
    "alternative_explanations": [
      "Premature completion/planning or instruction adherence, not demonstrated value decay."
    ],
    "proposed_discriminator": "Use the fixed matched task with serial versus concurrent retrieval and retain server plus ATIF ordering; independently score final binding and retrieval protocol. Requires approval before execution.",
    "confidence": "high for observable mechanics; low for causal attribution"
  },
  {
    "schema_version": 1,
    "analysis_id": "4b6b257e-27e4-5910-b1ae-3697896733cd",
    "source_trial_id": "cc034668-9ed7-4431-8fac-56e9eba11e09",
    "source_digests": {
      "result": "sha256:d8e527e48887d5c82ee98d114105e7c9556940eb8b4b5b5ceada4a7a9398e463",
      "trajectory": "sha256:abe18c46533b720e55a6fd2d1ec68c1df9e72094537a61d338bdab8e9f9a121d",
      "task": "sha256:add939bcbff3761fa60b8c60c6ad602ef4c26c8ea61bca6afda079c5ead1750b",
      "task_digest_status": "Recorded lock digest; original runtime task absent, not independently rehashed"
    },
    "analysis_provenance_ref": "report.analysis_provenance",
    "validity": "valid_agent_attempt_with_provenance_limits",
    "primary_category": "unknown",
    "summary": "Sequential neutral counterexample: all 257 reads in exact order and reward 1.0.",
    "evidence": [
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold-t3/action-64k-neutral_padding-s1337__u4CZxsA/verifier/result.json",
        "supports": "exact_latest_value_bound_after_complete_retrieval"
      },
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold-t3/action-64k-neutral_padding-s1337__u4CZxsA/artifacts/app/output/benchmark-events.jsonl",
        "first_divergent_read_ordinal": null,
        "supports": "257/257 unique expected handles; 257 logical reads; 0 duplicates; 0 application-level not_found"
      },
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold-t3/action-64k-neutral_padding-s1337__u4CZxsA/agent/trajectory.json",
        "step_id": 260,
        "tool_call_id": "call_c93384eafb79406ba9ccfb25",
        "supports": "Mutation exactly matches entity/attribute/latest token in retrieved CRITICAL STATE INVERSION record."
      }
    ],
    "alternative_explanations": [
      "One seed/attempt cannot establish scaffold effectiveness."
    ],
    "proposed_discriminator": "Use the fixed matched task with serial versus concurrent retrieval and retain server plus ATIF ordering; independently score final binding and retrieval protocol. Requires approval before execution.",
    "confidence": "high for observable mechanics; low for causal attribution"
  },
  {
    "schema_version": 1,
    "analysis_id": "84e4904d-cb12-56fc-9aee-f8d98ef2f199",
    "source_trial_id": "477a5527-c4f7-4667-92d3-e892c556f6a1",
    "source_digests": {
      "result": "sha256:e459793a5d902682b429424f7c8029cde779a0c9672fe51f19b7cdf1eaeb20f8",
      "trajectory": "sha256:f4b91cbc876be00ed466b03c67d9a1f09ac7dc90191dbce59a2782030dab78ce",
      "task": "sha256:add939bcbff3761fa60b8c60c6ad602ef4c26c8ea61bca6afda079c5ead1750b",
      "task_digest_status": "Recorded lock digest; original runtime task absent, not independently rehashed"
    },
    "analysis_provenance_ref": "report.analysis_provenance",
    "validity": "harness_failure",
    "primary_category": "harness_failure",
    "summary": "AgentTimeoutError interrupts sequential reading after 88 of 257 reads, before any mutation.",
    "evidence": [
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold/action-64k-neutral_padding-s1337__VxJbtpZ/verifier/result.json",
        "supports": "missing_final_state_evidence"
      },
      {
        "path": "research/evidence/runs/zai-wave2-action64k-s1337-sequential-scaffold/action-64k-neutral_padding-s1337__VxJbtpZ/artifacts/app/output/benchmark-events.jsonl",
        "first_divergent_read_ordinal": 89,
        "supports": "88/257 unique expected handles; 88 logical reads; 0 duplicates; 0 application-level not_found"
      }
    ],
    "alternative_explanations": [
      "Budget exhaustion is infrastructure/budget evidence, not a capability denominator."
    ],
    "proposed_discriminator": "Use the fixed matched task with serial versus concurrent retrieval and retain server plus ATIF ordering; independently score final binding and retrieval protocol. Requires approval before execution.",
    "confidence": "high for observable mechanics; low for causal attribution"
  }
]
```

## Appendix C — Complete trial ledger

Resolve each source as `research/evidence/runs/<job>/<trial>/`; job aliases are defined in §1. `latest_binding_matches` is blank for timeouts, because no mutation exists. `mechanism` uses exclusive precedence: timeout → pass → invalid handle → duplicate → incomplete → pure reorder. Application-level error counts inspect `result.value.error`, not the outer event flag. `length` is the historical k-label (4/16/64), not a token count; multiply by 1024 for byte dose. Full trial IDs are in the corresponding source `result.json` and representative sidecars.

```csv
job,trial,length,noise,seed,representation,reward,exception,steps,llm_calls,reads,expected_reads,coverage,duplicates,not_found,first_divergence,latest_binding_matches,mechanism
E0b,e0b-indexed-16k-neutral_padding__NeYp572,16,neutral_padding,1337,indexed,0.0,,6,5,65,65,65,0,0,8,True,complete_reordered
E0b,e0b-indexed-16k-neutral_padding__Q7YxsaN,16,neutral_padding,2026,indexed,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-indexed-16k-neutral_padding__auh7gKh,16,neutral_padding,42,indexed,0.0,,8,7,65,65,65,0,0,3,True,complete_reordered
E0b,e0b-indexed-16k-neutral_padding__eDfHfz4,16,neutral_padding,2026,indexed,0.0,,8,7,65,65,65,0,0,7,True,complete_reordered
E0b,e0b-indexed-16k-neutral_padding__hgSB2wg,16,neutral_padding,42,indexed,1.0,,6,5,65,65,65,0,0,,True,pass
E0b,e0b-indexed-16k-neutral_padding__tf6Qym8,16,neutral_padding,1337,indexed,0.0,,6,5,65,65,65,0,0,2,True,complete_reordered
E0b,e0b-indexed-16k-semantic_distrac__4maMAyy,16,semantic_distractor,1337,indexed,0.0,,8,7,65,65,65,0,0,2,True,complete_reordered
E0b,e0b-indexed-16k-semantic_distrac__7Bqqk82,16,semantic_distractor,42,indexed,0.0,,8,7,65,65,65,0,0,5,True,complete_reordered
E0b,e0b-indexed-16k-semantic_distrac__LPNcqqi,16,semantic_distractor,2026,indexed,0.0,,8,7,65,65,65,0,0,3,True,complete_reordered
E0b,e0b-indexed-16k-semantic_distrac__WevxUwT,16,semantic_distractor,1337,indexed,0.0,,8,7,65,65,65,0,0,15,True,complete_reordered
E0b,e0b-indexed-16k-semantic_distrac__YQqrniW,16,semantic_distractor,2026,indexed,1.0,,7,6,65,65,65,0,0,,True,pass
E0b,e0b-indexed-16k-semantic_distrac__i54upuG,16,semantic_distractor,42,indexed,0.0,,6,5,65,65,65,0,0,9,True,complete_reordered
E0b,e0b-indexed-4k-neutral_padding-s__2MCxugw,4,neutral_padding,42,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-indexed-4k-neutral_padding-s__DuTboZH,4,neutral_padding,1337,indexed,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-indexed-4k-neutral_padding-s__DzdyHn5,4,neutral_padding,42,indexed,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-indexed-4k-neutral_padding-s__HvkBNAn,4,neutral_padding,1337,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-indexed-4k-neutral_padding-s__jkHTpa5,4,neutral_padding,2026,indexed,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-indexed-4k-neutral_padding-s__pVv7NZn,4,neutral_padding,2026,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-indexed-4k-semantic_distract__AdQaFPM,4,semantic_distractor,1337,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-indexed-4k-semantic_distract__AiBa5mE,4,semantic_distractor,1337,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-indexed-4k-semantic_distract__L3MruNW,4,semantic_distractor,42,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-indexed-4k-semantic_distract__W4Mo2XQ,4,semantic_distractor,42,indexed,0.0,,5,4,17,17,17,0,0,8,True,complete_reordered
E0b,e0b-indexed-4k-semantic_distract__fuASozZ,4,semantic_distractor,2026,indexed,0.0,,5,4,17,17,17,0,0,16,True,complete_reordered
E0b,e0b-indexed-4k-semantic_distract__svsaFvT,4,semantic_distractor,2026,indexed,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-opaque-16k-neutral_padding-s__46pfu8g,16,neutral_padding,1337,opaque,1.0,,6,5,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-neutral_padding-s__5sDeq7o,16,neutral_padding,2026,opaque,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-neutral_padding-s__6LqrTuj,16,neutral_padding,42,opaque,1.0,,8,7,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-neutral_padding-s__Gdtq5L6,16,neutral_padding,1337,opaque,1.0,,9,8,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-neutral_padding-s__u3Dyqqb,16,neutral_padding,2026,opaque,0.0,,6,5,65,65,65,0,0,2,True,complete_reordered
E0b,e0b-opaque-16k-neutral_padding-s__xFUmWJJ,16,neutral_padding,42,opaque,1.0,,7,6,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-semantic_distract__35HqUif,16,semantic_distractor,2026,opaque,0.0,,7,6,65,65,65,0,0,2,True,complete_reordered
E0b,e0b-opaque-16k-semantic_distract__D4N6uDW,16,semantic_distractor,2026,opaque,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-semantic_distract__SQHzhJg,16,semantic_distractor,1337,opaque,0.0,,5,4,65,65,65,0,0,6,True,complete_reordered
E0b,e0b-opaque-16k-semantic_distract__SrUwFte,16,semantic_distractor,42,opaque,0.0,,8,7,65,65,65,0,0,3,True,complete_reordered
E0b,e0b-opaque-16k-semantic_distract__nVwjyai,16,semantic_distractor,1337,opaque,1.0,,7,6,65,65,65,0,0,,True,pass
E0b,e0b-opaque-16k-semantic_distract__vXfiLjM,16,semantic_distractor,42,opaque,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-opaque-4k-neutral_padding-s1__AujWXut,4,neutral_padding,1337,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-neutral_padding-s1__MuVGwid,4,neutral_padding,1337,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-neutral_padding-s2__rNBAWPb,4,neutral_padding,2026,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-neutral_padding-s2__yfkBcTX,4,neutral_padding,2026,opaque,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
E0b,e0b-opaque-4k-neutral_padding-s4__CwRopyC,4,neutral_padding,42,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-neutral_padding-s4__QSabEuT,4,neutral_padding,42,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-semantic_distracto__A2pv2hn,4,semantic_distractor,1337,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-semantic_distracto__KEpnoi6,4,semantic_distractor,2026,opaque,0.0,,5,4,17,17,17,0,0,8,True,complete_reordered
E0b,e0b-opaque-4k-semantic_distracto__joJjLye,4,semantic_distractor,42,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-semantic_distracto__puZEPeV,4,semantic_distractor,1337,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-semantic_distracto__r9Amt5i,4,semantic_distractor,2026,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-opaque-4k-semantic_distracto__tYab62M,4,semantic_distractor,42,opaque,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-16k-neutral_padd__AcTx6Cm,16,neutral_padding,42,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-neutral_padd__Nj8s3us,16,neutral_padding,1337,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-neutral_padd__VN3nRkV,16,neutral_padding,2026,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-neutral_padd__eDEuXGw,16,neutral_padding,42,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-neutral_padd__ju8LNVm,16,neutral_padding,1337,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-neutral_padd__kNHo7hh,16,neutral_padding,2026,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-semantic_dis__3P9pnPm,16,semantic_distractor,2026,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-semantic_dis__5TR7eTF,16,semantic_distractor,42,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-semantic_dis__6NRjbVx,16,semantic_distractor,2026,range_batch,0.0,,6,5,66,65,65,1,0,66,True,duplicate_reads
E0b,e0b-range_batch-16k-semantic_dis__bsE2ymC,16,semantic_distractor,42,range_batch,1.0,,5,4,65,65,65,0,0,,True,pass
E0b,e0b-range_batch-16k-semantic_dis__cLbnVbw,16,semantic_distractor,1337,range_batch,0.0,,6,5,66,65,65,1,0,66,True,duplicate_reads
E0b,e0b-range_batch-16k-semantic_dis__covk4HK,16,semantic_distractor,1337,range_batch,0.0,,6,5,66,65,65,1,0,66,True,duplicate_reads
E0b,e0b-range_batch-4k-neutral_paddi__4pfh2be,4,neutral_padding,42,range_batch,0.0,,6,5,18,17,17,1,0,18,True,duplicate_reads
E0b,e0b-range_batch-4k-neutral_paddi__UrmeW3N,4,neutral_padding,1337,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-neutral_paddi__jSP5eqH,4,neutral_padding,42,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-neutral_paddi__u4ktUm5,4,neutral_padding,2026,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-neutral_paddi__vrAt2J8,4,neutral_padding,1337,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-neutral_paddi__zYJVLyW,4,neutral_padding,2026,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-semantic_dist__6gJnMqy,4,semantic_distractor,42,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-semantic_dist__9FGh6A5,4,semantic_distractor,2026,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-semantic_dist__AxV4JJt,4,semantic_distractor,42,range_batch,0.0,,6,5,18,17,17,1,0,18,True,duplicate_reads
E0b,e0b-range_batch-4k-semantic_dist__PaLUYJX,4,semantic_distractor,1337,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-semantic_dist__tyvF2yt,4,semantic_distractor,2026,range_batch,1.0,,5,4,17,17,17,0,0,,True,pass
E0b,e0b-range_batch-4k-semantic_dist__wrQTy77,4,semantic_distractor,1337,range_batch,0.0,,6,5,18,17,17,1,0,18,True,duplicate_reads
Phase-A,action-16k-neutral_padding-s1337__RyhboUY,16,neutral_padding,1337,baseline,1.0,,5,4,65,65,65,0,0,,True,pass
Phase-A,action-16k-neutral_padding-s1337__WNCTjTp,16,neutral_padding,1337,baseline,1.0,,6,5,65,65,65,0,0,,True,pass
Phase-A,action-16k-neutral_padding-s2026__2dvmBtq,16,neutral_padding,2026,baseline,1.0,,7,6,65,65,65,0,0,,True,pass
Phase-A,action-16k-neutral_padding-s2026__oZvu6eF,16,neutral_padding,2026,baseline,1.0,,5,4,65,65,65,0,0,,True,pass
Phase-A,action-16k-neutral_padding-s42__8nicjhA,16,neutral_padding,42,baseline,0.0,,7,6,65,65,65,0,0,2,True,complete_reordered
Phase-A,action-16k-neutral_padding-s42__wAwQXC3,16,neutral_padding,42,baseline,1.0,,7,6,65,65,65,0,0,,True,pass
Phase-A,action-16k-semantic_distractor-s__7vQAEX6,16,semantic_distractor,42,baseline,0.0,,7,6,65,65,65,0,0,6,True,complete_reordered
Phase-A,action-16k-semantic_distractor-s__WMfL9Ed,16,semantic_distractor,1337,baseline,0.0,,6,5,65,65,65,0,0,3,True,complete_reordered
Phase-A,action-16k-semantic_distractor-s__f4iZbmp,16,semantic_distractor,2026,baseline,1.0,,7,6,65,65,65,0,0,,True,pass
Phase-A,action-16k-semantic_distractor-s__jzVivDR,16,semantic_distractor,1337,baseline,1.0,,8,7,65,65,65,0,0,,True,pass
Phase-A,action-16k-semantic_distractor-s__qA779fh,16,semantic_distractor,42,baseline,1.0,,7,6,65,65,65,0,0,,True,pass
Phase-A,action-16k-semantic_distractor-s__w4D7Fk7,16,semantic_distractor,2026,baseline,1.0,,5,4,65,65,65,0,0,,True,pass
Phase-A,action-4k-neutral_padding-s1337__7oQNAeC,4,neutral_padding,1337,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-neutral_padding-s1337__c29mo9d,4,neutral_padding,1337,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-neutral_padding-s2026__45EV9kt,4,neutral_padding,2026,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-neutral_padding-s2026__g7DNGyu,4,neutral_padding,2026,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-neutral_padding-s42__fHCW29G,4,neutral_padding,42,baseline,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
Phase-A,action-4k-neutral_padding-s42__huvDBVJ,4,neutral_padding,42,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-semantic_distractor-s1__V9ZDDrL,4,semantic_distractor,1337,baseline,0.0,,5,4,17,17,17,0,0,2,True,complete_reordered
Phase-A,action-4k-semantic_distractor-s1__eUGnBBL,4,semantic_distractor,1337,baseline,0.0,,5,4,17,17,17,0,0,9,True,complete_reordered
Phase-A,action-4k-semantic_distractor-s2__LsbUJ8W,4,semantic_distractor,2026,baseline,0.0,,5,4,17,17,17,0,0,3,True,complete_reordered
Phase-A,action-4k-semantic_distractor-s2__RYbw3qU,4,semantic_distractor,2026,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-semantic_distractor-s4__Zu9iVdi,4,semantic_distractor,42,baseline,1.0,,5,4,17,17,17,0,0,,True,pass
Phase-A,action-4k-semantic_distractor-s4__zqVomHN,4,semantic_distractor,42,baseline,0.0,,5,4,17,17,17,0,0,11,True,complete_reordered
Phase-A,action-64k-neutral_padding-s1337__FFSbF3C,64,neutral_padding,1337,baseline,0.0,,18,17,257,257,256,0,1,84,True,invalid_handle
Phase-A,action-64k-neutral_padding-s1337__wPckzoW,64,neutral_padding,1337,baseline,0.0,,16,15,258,257,256,1,2,84,True,invalid_handle
Phase-A,action-64k-neutral_padding-s2026__bbfDJyU,64,neutral_padding,2026,baseline,1.0,,14,13,257,257,257,0,0,,True,pass
Phase-A,action-64k-neutral_padding-s2026__rHaqFij,64,neutral_padding,2026,baseline,0.0,,19,18,259,257,256,2,2,20,True,invalid_handle
Phase-A,action-64k-neutral_padding-s42__EpUT8FT,64,neutral_padding,42,baseline,1.0,,14,13,257,257,257,0,0,,True,pass
Phase-A,action-64k-neutral_padding-s42__KJNRb6i,64,neutral_padding,42,baseline,0.0,,21,20,257,257,257,0,0,244,True,complete_reordered
Phase-A,action-64k-semantic_distractor-s__3sAERpi,64,semantic_distractor,1337,baseline,0.0,,14,13,257,257,256,0,1,84,True,invalid_handle
Phase-A,action-64k-semantic_distractor-s__6vDNEHZ,64,semantic_distractor,42,baseline,0.0,,13,12,264,257,257,7,0,7,True,duplicate_reads
Phase-A,action-64k-semantic_distractor-s__8aYeUds,64,semantic_distractor,1337,baseline,0.0,,15,14,522,257,257,264,1,5,True,invalid_handle
Phase-A,action-64k-semantic_distractor-s__GH67Boh,64,semantic_distractor,42,baseline,0.0,,16,15,258,257,257,1,0,258,True,duplicate_reads
Phase-A,action-64k-semantic_distractor-s__eFWYpcQ,64,semantic_distractor,2026,baseline,1.0,,13,12,257,257,257,0,0,,True,pass
Phase-A,action-64k-semantic_distractor-s__uEydwLT,64,semantic_distractor,2026,baseline,0.0,,18,17,258,257,257,0,1,116,True,invalid_handle
Repeats,action-64k-neutral_padding-s1337__JvdEs9Y,64,neutral_padding,1337,baseline,0.0,,18,17,257,257,256,0,1,84,True,invalid_handle
Repeats,action-64k-neutral_padding-s1337__wCHLZ4M,64,neutral_padding,1337,baseline,0.0,,11,10,257,257,256,0,1,3,True,invalid_handle
Repeats,action-64k-semantic_distractor-s__FLiG7jy,64,semantic_distractor,1337,baseline,0.0,,16,15,257,257,256,0,1,3,True,invalid_handle
Repeats,action-64k-semantic_distractor-s__Pgukjp8,64,semantic_distractor,1337,baseline,0.0,,18,17,259,257,256,2,2,84,True,invalid_handle
Sequential-default,action-64k-neutral_padding-s1337__VxJbtpZ,64,neutral_padding,1337,baseline,0.0,AgentTimeoutError,90,89,88,257,88,0,0,89,,harness_failure
Sequential-default,action-64k-semantic_distractor-s__TyoghGd,64,semantic_distractor,1337,baseline,0.0,AgentTimeoutError,86,85,84,257,83,0,1,84,,harness_failure
Sequential-t3,action-64k-neutral_padding-s1337__u4CZxsA,64,neutral_padding,1337,baseline,1.0,,261,260,257,257,257,0,0,,True,pass
Sequential-t3,action-64k-semantic_distractor-s__A67eDZ2,64,semantic_distractor,1337,baseline,0.0,,236,235,232,257,232,0,0,233,True,incomplete_retrieval
```

## Appendix D — Reproduction

Run the following standard-library Python from the repository root (no network, paid calls, mutation, database access, or project test suite). It recomputes every cell pass/step curve, checks reward consistency and promotion hashes, independently checks final binding, and regenerates the corpus fingerprint. The logical-read details in Appendix C were extracted by flattening single reads, explicit batch lists, and inclusive batch ranges in server event order; errors inspect nested `value.error`.

```python
import collections, hashlib, json, re, statistics
from pathlib import Path
root = Path.cwd()
prefixes = ("zai-overnight-action-phase-a-v2", "zai-e0b-handle-representation-r2", "zai-wave2-action64k")
jobs = sorted(p for p in (root / "research/evidence/runs").iterdir() if p.name.startswith(prefixes))
cells, inventory = collections.defaultdict(list), []
count = correct = mutations = exceptions = checked = 0
for job in jobs:
    for entry in json.loads((job / "PROMOTION.json").read_text())["files"]:
        if entry.get("promoted_path") and entry.get("promoted_sha256"):
            actual = "sha256:" + hashlib.sha256((job / entry["promoted_path"]).read_bytes()).hexdigest()
            assert actual == entry["promoted_sha256"]
            checked += 1
    for result_path in sorted(job.glob("*/result.json")):
        trial = result_path.parent
        result = json.loads(result_path.read_text())
        trajectory = json.loads((trial / "agent/trajectory.json").read_text())
        verifier = json.loads((trial / "verifier/result.json").read_text())
        reward = result["verifier_result"]["rewards"]["reward"]
        assert reward == verifier["reward"] == float((trial / "verifier/reward.txt").read_text())
        task = Path(result["config"]["task"]["path"]).name
        match = re.search(r"(4|16|64)k-(neutral_padding|semantic_distractor)-s(\d+)", task)
        representation = task.split("-")[1] if task.startswith("e0b-") else "baseline"
        exception = bool(result.get("exception_info"))
        cells[(job.name, representation, int(match[1]), match[2])].append(
            (reward, exception, len(trajectory["steps"])))
        events = [json.loads(line) for line in (trial / "artifacts/app/output/benchmark-events.jsonl").read_text().splitlines()]
        contents, finals = [], []
        for event in events:
            value = event["result"].get("value", {})
            if event["tool_name"] == "get_context_chunk":
                contents.append(value.get("content", ""))
            elif event["tool_name"] == "get_context_chunks":
                contents.extend(c.get("content", "") for c in value.get("chunks", []))
            elif event["tool_name"] == "execute_mutation":
                finals.append(value)
        if finals:
            override = next(c for c in contents if "CRITICAL STATE INVERSION" in c)
            latest = re.search(r"attribute (\w+) is now mutated to '([^']+)'", override)
            entity = re.search(r"OVERRIDE event for (\w+)\.", override)
            correct += len(finals) == 1 and finals[0]["bound_value"] == latest[2] and finals[0]["target_attribute"] == latest[1] and finals[0]["target_entity"] == entity[1]
            mutations += 1
        exceptions += exception
        count += 1
        for rel in ("result.json", "agent/trajectory.json", "lock.json", "config.json", "verifier/result.json", "artifacts/app/output/benchmark-events.jsonl"):
            p = trial / rel
            inventory.append({"path": str(p.relative_to(root)), "sha256": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()})
for key, group in sorted(cells.items()):
    scored = [reward for reward, excluded, steps in group if not excluded]
    steps = [steps for reward, excluded, steps in group]
    print(key, "pass/scored", sum(r == 1 for r in scored), len(scored), "steps mean/median/min/max", statistics.mean(steps), statistics.median(steps), min(steps), max(steps))
encoded = json.dumps(sorted(inventory, key=lambda x: x["path"]), sort_keys=True, separators=(",", ":")).encode()
print("corpus", "sha256:" + hashlib.sha256(encoded).hexdigest())
print("trials/exclusions/binding_matches/mutations/manifest_files", count, exceptions, correct, mutations, checked)
```
