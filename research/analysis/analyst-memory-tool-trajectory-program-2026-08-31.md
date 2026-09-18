---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-31
license_note: Internal Analyst analysis over Eval Lab artifacts; repository license governs.
status: distilled
feeds:
  - parked
---

# Memory, tool use, and trajectory evidence — analysis program

**From:** Analyst (Trajectory Intelligence) · **Brief:** `research/inbox/analyst-memory-tool-trajectory-program.md`
**Baseline read:** `67b4051a` (read-only, isolated worktree) · **Date:** 2026-08-31
**Convention:** `[OBSERVED]` = executed or read at `67b4051a` · `[DERIVED]` = computed here · `[INFERENCE]` = judgement · `[FORECAST]` = about future runs

> **Baseline provenance caveat.** `[OBSERVED]` `67b4051a` is **not an ancestor of `origin/main`** (`93d2e7c1`) — it is a divergent branch head, 0 commits behind main by rev-list. Every finding below is pinned to `67b4051a`. Anyone reproducing from `main` may see different results.

---

## TL;DR — the brief asks the wrong question, and the real answer is better news

The brief frames the gap as *"hypotheses that require real model trajectories."* `[OBSERVED]` **Real model trajectories already exist: 164 trials across 3 models.** The blocker is not missing runs.

| Finding | Number |
|---|---:|
| Promoted trial directories with artifacts | **170** |
| …that carry a benchmark **contract** file | **0** |
| …that load via `load_trial_bundle` | **0 / 170** |
| Benchmark contract files anywhere in the repo | **1** (the `action-memory-v1` template) |
| `load_trial_bundle` callers outside its own module | **3, all in `tests/`** |
| `extract_memory_continuity_features` non-test callers | **0** |
| Phase-A summary keys traceable to repository code | **0 of 5** |

### Aligned to the Architect review

`[OBSERVED]` Aligned to `/tmp/eval-lab-program-topology-review-67b4051a.md` on all five requested points:

| Requested alignment | Where | Change from my first draft |
|---|---|---|
| Minimum comparable-trial envelope | §2b | New — measured all 9 elements; element 4 fails 0/170 |
| Treatment-correlated capture-loss checks | §7 | Promoted from diagnostic to **mandatory pre-claim gate** with a worked refusal |
| Explicit denominators | §7 | New register; adds **gold vs inferred** per ADR-020 |
| Independent base-task/seed units | §6 | Unit is now **independent seed**, cluster key **`base_task_pair_id`** |
| Paired memory × tool-dependency 2×2 | §6 | Architect's factor levels adopted verbatim; vector of 4 outcomes; DiD on success **and** conformance separately |
| **Admissibility of the 128 newly-loadable trials** | **§6c** | **Calibration-only.** Darwin allowlisting is not enforceable (`allowlist_enforced=False` by construction). `evidence_class` is in 5 summaries and **0/170 trials** — new gate G7 refuses analysis without resolved provenance |
| 2×2 design status | **§6** | **`dependency_hold` at `c8978a65`** — `measurement_authorized: false`. Class D design, **not evidence**. Exit needs `tool_schema_digest` (the §6b finding) + stable action-memory task identity |
| First concrete paired unit | **§6b** | `am-state-inversion-canary-s42-4k` @ **`95bb3a98`** adopted as paired unit #1 — **control, not behaviour**. B1–B5 verified: byte parity and diff allowlist are *enforced*; `tool_schema_digest` is *declared only*; 6/6 reproduced |

`[INFERENCE]` **There are two parallel analysis paths, and the reports people read come from the ungoverned one.** The governed path — 240 registered features, `verdict_coupling`, denominator policies, refusal codes — consumes `TrialBundle` and has never touched a promoted trial. The path that produced the Phase-A and calibration summaries is not in the repository at all.

That reframes the program: the first work is **evidence-promotion repair**, not model runs.

---

## §1 — Evidence inventory and admissibility

`[OBSERVED]` Enumerated from job-level `result.json` rollups (`stats.evals.<agent>__<model>__<suite>`), which is the only place model identity is recorded.

### 1.1 Lanes

| Class | Lanes | Trials | Detail |
|---|---:|---:|---|
| **REAL MODEL** | 18 | **164** | `glm-5.3-flash` (majority), `glm-5.3`, `gpt-5.6-terra` |
| **CONTROL** | 6 | **6** | `oracle` ×3, `nop` ×3 — event-summary, deepplanning-travel-lisbon-registry, syn-funcdag-easy-registry |

`[OBSERVED]` Largest real lanes: `zai-e0b-handle-representation-r2` (72), `zai-overnight-action-phase-a-v2` (36), `zai-wave2-flash-matrix` (17). Recovery is 6 trials: 3 `recovery-clean` twins + 3 `recovery` transient-5xx faults.

### 1.2 Admissibility classes

| Class | What it licenses | Present |
|---|---|---|
| **A — Real behaviour, governed** | Capability claims through the registered feature layer | **0 trials.** No promoted trial loads. |
| **B — Real behaviour, ungoverned** | Descriptive claims, with the generator's provenance stated | 164 trials, via summaries whose generator is absent (§1.4) |
| **C — Deterministic control** | Validates the *instrument*, never the model | 6 oracle/nop trials; MCP oracle/blind-retry contract |
| **D — Contract/design only** | Specifies what *would* be measured | MCP 20-cell design; memory continuity producer; **the action-memory control canary (§6b)** |
| **E — Absent** | Nothing | LoCoMo (§1.5, §6d) — still 0 paths at program HEAD `8e02fba2` |
| **F — Candidate registry state** | **Nothing.** A proposal, not a package | `registry.py` `candidate` rows (§6d) — queryable, looks like inventory, is not |

`[INFERENCE]` The brief's controls are correctly labelled — *"These validate the control, not model capability"* is exactly right and I have nothing to add. The unlabelled risk is class **B**: 164 real trials whose numbers are real but whose derivation is not reproducible.

### 1.3 Verified: the MCP 20-cell arithmetic

`[OBSERVED]` `library/benchmarks/mcp-recovery-v1/contract.py` — `CAMPAIGN0_PERSISTENCE = (1, 2)` and five `FaultClass` members with designated repair moves:

| Fault class | Designated repair |
|---|---|
| `PERSISTENT_SIGNATURE_ERROR` | `refresh_auth` |
| `PERSISTENT_SCHEMA_MISMATCH` | `fallback_query` |
| `TRANSIENT_NETWORK_TIMEOUT` | `refresh_auth` |
| `TRANSIENT_HTTP_5XX` | `fallback_query` |
| `SILENT_WRONG_PAYLOAD` | `fallback_query` |

$$5 \text{ faults} \times 2 \text{ persistence} = 10 \text{ fault cells} \;+\; 10 \text{ clean twins} = \mathbf{20}$$

`[DERIVED]` The brief's cell count reconciles exactly. `ALTERNATIVE_REPAIR_MOVES` also exists, which means wrong-repair mutants are constructible — a negative control the brief did not claim and should (§5).

### 1.4 The Phase-A summary is not reproducible from repository code

`[OBSERVED]` Each distinctive summary key appears in **0 code files** and exactly **1 data file** (the summary itself):

`paired_arm_contrasts` · `order_fidelity_reward_equivalence` · `repeat_stability` · `atif_event_order_mismatch_trials` · `coverage_complete_trials`

`[OBSERVED]` `zai_report.py` (337 lines) reads `benchmark-events.jsonl` but contains none of these keys. `[INFERENCE]` So the summary was produced by an uncommitted script or by hand. The 36 raw trial directories exist with events and final-state, so it *is* regenerable — but nothing in the repository regenerates it. **Every number in the two feature-analysis reports, including mine, inherits this.**

### 1.5 LoCoMo does not exist

`[OBSERVED]` Paths matching `locomo`/`LoCoMo`: **0** in the worktree, **0** on `origin/main`, **0** on `HEAD`, and no commit on any ref ever added one.

`[INFERENCE]` The brief lists under *Current Evidence*: *"LoCoMo conversation 26: 199 questions. Oracle reward 1.0 and nop reward 0.0."* I cannot verify any part of that from this repository. It is either in another workspace, or it ran without promotion. **Until it is promoted, treat it as class E (absent), not class C (control).** The distinction matters because §2 and §7 are written against "one real LoCoMo canary" as the trigger, and there is currently no LoCoMo task to run a canary on.

---

## §2 — The blocker, precisely: two disconnected paths

`[OBSERVED]` `load_trial_bundle` requires three artifacts. Across 170 promoted trial dirs:

| Required artifact | Present |
|---|---:|
| benchmark contract (`benchmark_contract.json` / `contract.json` / nested) | **0 / 170** |
| benchmark events (`benchmark-events.jsonl` / nested) | 152 / 170 |
| final state (`final-state.json` / nested) | 128 / 170 |
| **Loads successfully** | **0 / 170** — all `BenchmarkMissingArtifactError` |

`[OBSERVED]` I ran the loader on three real trials to isolate code from evidence. All three fail identically, including Phase-A — so this is **not** recovery-specific:

```
[PHASE-A action] BenchmarkMissingArtifactError: contract file not found
[RECOVERY fault] BenchmarkMissingArtifactError: contract file not found
[RECOVERY clean] BenchmarkMissingArtifactError: contract file not found
```

`[OBSERVED]` Caller topology:

| Symbol | Non-test callers |
|---|---|
| `load_trial_bundle` | **none** (3 callers, all `tests/`) |
| `extract_action_memory_features` | `cli.py`, `traj_card.py` |
| `extract_mcp_recovery_features` | `cli.py`, `traj_card.py` |
| `extract_mcp_funcdag_features` | `cli.py`, `traj_card.py` |
| `extract_memory_continuity_features` | **none** |

`[INFERENCE]` So three benchmark producers have a CLI entry point with no ingestible input, and the memory producer has neither. The governed feature layer is reachable only from tests.

### The two lanes, stated plainly

| | Governed lane | Report lane |
|---|---|---|
| Input | `TrialBundle` (strict, 3 artifacts) | `benchmark-events.jsonl` read directly |
| Features | 240 registered, `verdict_coupling`, denominators, refusals | ad-hoc keys |
| Promoted trials it can read | **0** | 164 |
| Reproducible from repo | yes | **no** (§1.4) |
| Produced the reports we cite | **no** | yes |

`[INFERENCE]` This is the single highest-value fix in the program and it is a **promotion-contract change, not a modelling change**: emit `benchmark_contract.json` per trial at promotion time. It converts 164 existing real trials from class B to class A retroactively, at zero model cost.

---

## §2b — Measured against the minimum comparable-trial envelope

`[OBSERVED]` The Architect review defines a **minimum comparable-trial envelope**: a required tuple of nine existing artifacts, explicitly *not* a new mega-schema. I measured the promoted evidence against each element at `67b4051a`.

| # | Envelope element | Status at `67b4051a` |
|---:|---|---|
| 1 | **Execution identity** — campaign/manifest digest, cell, attempt, repeat seed, task, model, agent, harness, scaffold, dose axis/value/unit, alphabet, **`base_task_pair_id`** | Partial. Model identity lives only in job-level `stats.evals.<agent>__<model>__<suite>`; not on the trial record. |
| 2 | **Runtime identity** — profile ID, qualification digest, adapter/OpenCode/model pins, credential transport, ceilings | **Absent.** No OpenCode/Z.ai `AgentProfile` is declared (Architect's blocker 1). |
| 3 | **Task authority** — task package digest, verifier/oracle digest, generator/version/seed, license | Present in summaries (`task_digest`, `verifier_digest`, `metric_config_digest`). |
| 4 | **Raw evidence** — immutable `TrialBundle`, CAS URI, ATIF digest, redaction digest | **0 / 170 trials satisfy this.** No promoted trial carries a benchmark contract; `load_trial_bundle` fails on all of them. |
| 5 | **Outcome authority** — `OutcomeRecord`, composite validity axes, supersession/regrade lineage | Present and validated on BBO / Game2048. |
| 6 | **Data readiness** — capture/quality status, typed refusal reasons; *missing never coerced to zero* | Contract present; the Phase-A synthetic `0.0` shows it is not yet universally enforced. |
| 7 | **Interpretation lineage** — `FactRow` source/digest/provenance, producer version, registry digest, denominator policy, timing, verdict coupling | Present in the registry; unreachable for promoted trials because element 4 fails. |
| 8 | **Analysis lineage** — snapshot digest, `CampaignAnalysisSpecV1`, **statistical unit/pair/cluster keys**, effective n / MDE / refusals | Contracts exist; not exercised on real evidence. |
| 9 | **Synthetic extension** | Out of scope for this brief. |

`[DERIVED]` **Element 4 is the binding constraint, and it fails universally.** The envelope's own first requirement on raw evidence is unmet by every promoted trial, which is why elements 7 and 8 cannot be reached even though their contracts are complete.

`[INFERENCE]` This is a **measurement of the Architect's envelope, not a disagreement with it.** The review states the spine exists — correct — and identifies two blockers at P0 (no declared profile, DeepSeek string gate). I add a third at P3: **freezing the envelope will not admit any existing trial until promotion emits the contract artifact.** That is one JSON file per trial and it retroactively converts 164 already-paid trials from ungoverned to governed.

### One correction to my own earlier position

`[OBSERVED]` The review lists under *Bureaucratic or premature*: *"requiring all legacy feature-governance backfills before a single canary."*

`[INFERENCE]` **That corrects me.** My earlier feature-analysis reply put the 72 temporal-availability and 73 denominator declarations at Stage A, ahead of everything. The Architect is right that those gate **comparative inference**, not **first ingestion** — a canary needs elements 1–6, and declarations only bind at element 7 when features become predictors. Revised position: promotion repair (element 4) and the extractor (§3) come first; the declaration backfill moves to just before the first comparison, not before the first trial.

---

## §3 — Memory continuity: a transform with no extractor

`[OBSERVED]` `memory_continuity.py` (214 lines) emits 18 well-formed features:

| Group | Features |
|---|---|
| Counts | `memory_write_count`, `memory_read_count`, `memory_use_count` |
| Linkage | `write_read_link_count`, `write_read_use_link_count` |
| Latency | `mean_write_to_read_latency_steps`, `mean_read_to_use_latency_steps` |
| Boundary | `context_boundary_count`, `boundary_carryover_opportunity_count`, `boundary_carryover_success_count`, `boundary_carryover_rate` |
| Position | `memory_read_context_position_observation_count`, `…_coverage`, `mean_…_tokens`, `max_…_tokens` |
| Status | `memory_continuity_status` |

`[OBSERVED]` It consumes `Sequence[ContextOperationFact]` with `operation ∈ {memory_write, memory_read, memory_use}`, `step_index`, `content_digest`, `operation_id`, and refuses on `missing_step_order` / `missing_content_identity`.

`[OBSERVED]` **`ContextOperationFact(` is constructed in exactly two places: `semantic_facts.py` (the schema) and `tests/test_memory_continuity_producer.py` (3 hand-authored facts).** No module derives these facts from a trajectory.

`[INFERENCE]` So the write→read→use chain has **never been computed from any real trajectory**, and cannot be until an ATIF → `ContextOperationFact` extractor exists. The producer is correct and well-specified; it is simply downstream of a hole. This is a *different* blocker from §2: recovery lacks an **artifact**, memory lacks an **extractor**.

**Two blockers, two fixes:**

| Lane | Blocker | Fix | Cost |
|---|---|---|---|
| Recovery / action-memory / funcdag | contract artifact not promoted | emit `benchmark_contract.json` at promotion | small, mechanical |
| Memory continuity | no ATIF → `ContextOperationFact` extractor | write the extractor with declared operation semantics | real design work |

---

## §4 — Metric specifications

`[INFERENCE]` For each, the denominator is the load-bearing part. Every rate below must emit **null on zero denominator**, never 0 — the discipline already demonstrated by Phase A's five null rates.

### 4.1 Write → read → explicit-use

| Metric | Formula | Denominator | Null when |
|---|---|---|---|
| `write_read_link_rate` | `write_read_link_count / memory_write_count` | writes | no write occurred |
| `read_use_link_rate` | `write_read_use_link_count / write_read_link_count` | linked reads | no read linked to a write |
| `end_to_end_use_rate` | `write_read_use_link_count / memory_write_count` | writes | no write occurred |
| `mean_write_to_read_latency_steps` | mean over linked pairs | linked pairs | no linked pair |

`[INFERENCE]` **`read_use_link_rate` is the only one of the three that isolates memory from retrieval.** A model that reads and ignores is behaviourally distinct from one that never reads, and only the conditional rate separates them. Report all three or the claim is ambiguous.

**Linkage must be identity-based, not positional.** `content_digest` already exists on the fact; require digest equality for a link, and record `link_basis="content_digest"`. A positional heuristic would make the metric an artifact of step ordering.

### 4.2 Stale binding

| Metric | Definition |
|---|---|
| `stale_read_count` | reads whose `content_digest` matches a superseded write for the same key |
| `stale_binding_survival_rate` | `stale_reads_that_reached_a_use / stale_read_count` |
| `latest_value_override_rate` | uses bound to the newest write / uses bound to any write |

`[OBSERVED]` The action-memory family already has `stale_value_override_rate` and `conflict_resolution_success`, both `verdict_coupling="defines"`. `[INFERENCE]` **So stale binding is outcome-defining for action-memory and must not be used as a predictor there.** For memory-continuity it can be a predictor only if its verifier does not condition on it — that determination is per-benchmark and must be recorded in `coupling_basis`, not assumed.

### 4.3 Boundary carryover

`boundary_carryover_rate = success / opportunity`, null when `opportunity = 0`.

`[INFERENCE]` **Opportunity-conditioning is the whole metric.** A trial with no context boundary has no carryover opportunity and must not contribute a 0 to the numerator or the denominator. The producer already models this with a separate `opportunity_count` — preserve that shape and never collapse to `success / boundary_count`.

### 4.4 Tool-call correctness

| Metric | Definition | Note |
|---|---|---|
| `tool_call_schema_valid_rate` | schema-valid calls / total calls | mechanical |
| `designated_move_rate` | designated repair calls / post-exposure calls | uses `DESIGNATED_REPAIR_MOVES` |
| `alternative_move_rate` | alternative repair calls / post-exposure calls | uses `ALTERNATIVE_REPAIR_MOVES` |
| `application_error_rate` | calls with application-level error / total | `is_application_error` exists |

### 4.5 Recovery after exposure

`[OBSERVED]` `mcp_recovery.py` emits `injected_fault_count`, `fault_detected_count`, `post_fault_retries`, `blind_retries`, `certified_recovered_faults`, plus `RecoveryPersistencePoint` and `build_recovery_persistence_curve`.

$$\text{exposure-conditioned recovery} = \frac{\texttt{certified\_recovered\_faults}}{\texttt{fault\_detected\_count}}$$

`[INFERENCE]` Three separate quantities that are routinely conflated, and all three are needed:

| Quantity | Denominator | Answers |
|---|---|---|
| Injected-conditioned | `injected_fault_count` | Did it recover from faults we caused? |
| **Exposure-conditioned** | `fault_detected_count` | Did it recover from faults it *noticed*? |
| Blind-retry share | `blind_retries / post_fault_retries` | Is "recovery" just retrying? |

`[OBSERVED]` `certified_recovered_faults` and `autonomous_recovery_rate` are `verdict_coupling="defines"`. **Neither may be a predictor of recovery outcome.** `blind_retries` and `post_fault_retries` are the admissible behavioural signals.

### 4.6 Failed-prefix cost

`[INFERENCE]` Define on the tokens and steps consumed **before** the first designated repair move:

`failed_prefix_steps`, `failed_prefix_tokens`, `failed_prefix_cost_usd`, and `failed_prefix_share = failed_prefix_tokens / total_prompt_tokens`.

This is the metric that distinguishes *eventually recovers* from *recovers efficiently*, and it is admissible because it is measured pre-verdict and is not part of any reward contract.

### 4.7 Outcome validity

`[OBSERVED]` The vector already validated on BBO and Game2048: `(agent_axis, verifier_axis, artifact_axis, authority_axis)`. Preserve BBO = agent-timed-out + verifier-valid; Game2048 = agent-timed-out + original-invalid + regrade-valid. `[INFERENCE]` Add `contract_axis` given §2: a trial whose contract artifact is absent is **not analysable**, which is a fourth independent failure mode currently invisible because nothing tries to load it.

---

## §5 — Negative controls

`[INFERENCE]` Each control must be able to *fail* and thereby retire a claim. Controls that cannot fail are decoration.

| # | Control | Isolates | Retires the claim if |
|---|---|---|---|
| 1 | **Read-without-use** | memory vs retrieval | `read_use_link_rate` is at chance while `write_read_link_rate` is high → the model retrieves without using |
| 2 | **Irrelevant-memory injection** | memory vs general capability | Outcome unchanged when injected memory is irrelevant *and* unchanged when relevant → memory content is not load-bearing |
| 3 | **State inversion** | genuine binding vs recency | Inverting which write is latest does not change the bound value → the model is using position, not state |
| 4 | **No-boundary twin** | carryover vs task difficulty | Matched task without a boundary shows the same failure rate → the failure is not carryover |
| 5 | **Tool-only, no retained state** | tool competence vs memory | Tool-call correctness equals the memory arm → the deficit is tool use, not memory |
| 6 | **Matched context-length** | memory vs context dilution | A neutral-padding arm at identical token count shows the same degradation → it is length, not semantic interference |
| 7 | **Wrong-repair mutant** | recovery vs any-action | `ALTERNATIVE_REPAIR_MOVES` scores as well as designated → "recovery" is not repair-specific |
| 8 | **Blind-retry baseline** | recovery vs retry | Model recovery is indistinguishable from blind retry `0/10` → no diagnosis occurred |
| 9 | **Oracle isolation** (ADR-020) | remembering vs reading | The target value is recoverable from the environment → the metric measures reading, not memory |
| 10 | **Frozen held-out generation** | memory vs curriculum drift | Effect vanishes on tasks frozen before the agent was observed → the effect was adaptation to observed failures |

`[OBSERVED]` Controls 6 and 8 partly exist: Phase A already has a `neutral_padding` vs `semantic_distractor` matched-dose axis, and the MCP contract has the blind-retry policy. `[INFERENCE]` Controls 1, 2, 3, 7 require new task variants. Control 4 requires a no-boundary twin generator.

---

## §6 — Paired memory × tool-dependency 2×2 — **`dependency_hold`, design only**

> `[OBSERVED]` **Status at `c8978a65`: `dependency_hold`.** `research/synthetic/memory-tool-2x2-design-v1.json` (`memory-tool-factorial-design/v1`, family `memory-tool-use-2x2-candidate-v1`) records `model_runs: 0`, `certification_runs: 0`, `registration_attempts: 0`, **`measurement_authorized: false`**, **`replication_authorized: false`**; `lineage.paired_lineage_spec_status: not_created_dependency_hold`; `partition.status: not_assigned_dependency_hold`; all lineage digests `null`.
>
> **This section is a design, not evidence.** It is **class D** in §1.2 and contributes to no numerator, denominator, rate, or claim. Nothing below may be cited as measurement.

`[OBSERVED]` Aligned to `/tmp/eval-lab-program-topology-review-67b4051a.md`. The Architect's factor levels are more precisely specified than my first draft and I adopt them verbatim; my draft's "memory support present/absent" was underspecified in exactly the way that admits a context-length confound.

| Factor | Level 0 | Level 1 |
|---|---|---|
| **Memory continuity** | prior fact cleared/unavailable, **with token-matched neutral result** | same fact written in session A and retrievable in session B |
| **Tool dependency** | matched current-session task solvable with the same tools/schema | same tool family **requires the prior-session fact** to choose/order/parameterize calls |

**Held constant:** exact profile/model/agent, harness/scaffold, context/token budget, tool inventory/schema/familiarization, base task family, verifier, generator version, partition, template, seed.

### Exit conditions: two pending upstream identities

`[OBSERVED]` The design names its own blockers as `required_upstream_identities`, both `status: pending`:

| Owner lane | Artifact | Required fields |
|---|---|---|
| **action-memory** | stable source task identity | `task_ref`, `source_task_digest`, `task_template_digest`, `verifier_digest`, `profile_digest` |
| **mcp-funcdag** | stable MCP/tool-use source task identity | `task_ref`, `source_task_digest`, **`tool_schema_digest`**, `tool_inventory_digest`, `verifier_digest` |

`[DERIVED]` **The MCP blocker is the finding from §6b.** `tool_schema_digest` is exactly the field I verified at `95bb3a98` as present in the canary spec but **computed in no code path and asserted in no test**. So the 2×2 cannot leave `dependency_hold` until that digest becomes enforced rather than declared — my B-finding sits directly on this hold's critical path, and closing it is a prerequisite, not a nicety.

`[INFERENCE]` The action-memory blocker is the same shape one level up: `source_task_digest` and `task_template_digest` require a **stable task identity**, which the promotion path cannot currently supply for the reason in §2 — no promoted trial carries a contract artifact. **Both exit conditions reduce to identity/digest stability, not to experiment design.** The design is finished; the substrate is not.

### Matched axes — adopt the design's list, it is a superset of mine

`[OBSERVED]` `matched_axes`: `context_length`, `tool_schema`, `tool_inventory`, `task_template`, `verifier_implementation`, `execution_profile`, `generator_version`, `seed`.

`[INFERENCE]` Eight axes against the six I listed as "held constant" — it adds `tool_inventory` and `verifier_implementation` explicitly. Use this list; drop mine.

### The SPADE boundary is now machine-checkable

`[OBSERVED]` `generator_calibration` records `executing_agent_memory_is_generator_memory: false`, `designer_history_and_hint_regret_use: "generator_curriculum_only"`, and `heldout_measurement_failures_used: false`.

`[DERIVED]` §8's SPADE correction — designer memory is not executing-agent memory — is no longer only prose. It is three boolean/enum fields that a validator can check. `[INFERENCE]` That is the right way to encode a citation-level boundary, and it is the first instance in the repo of the guard I asked for in §11 observation 6.

### Units: independent base-task/seed, clustered by base task pair

`[OBSERVED]` The Architect specifies: *"Randomize/counterbalance cell order within `base_task_pair_id`; cluster by base task pair and use independent seeds as the analysis units."*

`[INFERENCE]` This supersedes my draft's `(task, seed)` unit, and the difference is material:

| | My draft | Aligned |
|---|---|---|
| Analysis unit | `(task, seed)` | **independent seed** |
| Cluster key | not specified | **`base_task_pair_id`** |
| Cell order | not specified | randomized/counterbalanced **within** pair |
| Repeats | "not independent units" | same, and never pooled as tasks |

`[OBSERVED]` `base_task_pair_id` is already element 1 of the envelope (Execution identity) and `statistical unit/pair/cluster keys` is element 8 (Analysis lineage) — so both keys have a declared home and neither needs new schema.

`[DERIVED]` Why the cluster key matters here specifically: Phase A's arm contrast reported 18 pairs of which only 9 were independent, because repeats reused task content. Declaring `base_task_pair_id` as the cluster makes that collapse automatic rather than a post-hoc discovery.

### Primary outcomes stay a vector

`[OBSERVED]` Four outcomes, not one:

1. authoritative exact task success;
2. memory carryover success over **explicit opportunities**;
3. tool DAG/schema conformance;
4. certified recovery where a fault is injected.

### Interaction estimand

$$\Delta_{\text{int}} = \bigl[\Delta(\text{memory}) \mid \text{tool-dependent}\bigr] - \bigl[\Delta(\text{memory}) \mid \text{tool-matched control}\bigr]$$

`[OBSERVED]` Computed as a **paired difference-in-differences on exact success, and separately on opportunity-conditioned tool conformance** — two estimands, not one composite. `[INFERENCE]` Running it separately on conformance is the part that makes the result interpretable: exact success can move for reasons unrelated to tool behaviour, and conformance is the channel the hypothesis actually names.

### Sizing, and why this is not the first experiment

`[DERIVED]` Repository estimator at baseline $\approx 0.5$: paired $n=9 \Rightarrow \text{MDE} \approx 0.48$; $n=36 \Rightarrow 0.30$; $n=88 \Rightarrow 0.19$. A DiD interaction carries roughly twice the variance of a main effect, so a detectable interaction needs on the order of **4× the units** of a detectable main effect.

`[FORECAST]` A $2\times2$ at 9 independent seeds per cell cannot detect any plausible interaction. `[INFERENCE]` **Predeclare MDE and minimum informative paired units in `CampaignAnalysisSpecV1` and refuse inference when underpowered** — the Architect's rule, and it is the one that stops this design from producing a confident null. Run the memory main effect (cells A→B under tool-matched control) first; escalate to the interaction only if the main effect is real.

### Confound separation, mapped to controls

`[OBSERVED]` The Architect names three separations; each maps to a §5 control:

| Confound | Separator | My control |
|---|---|---|
| general tool ability | T0 tool arm + tool-familiarization canary | #5 tool-only |
| context length | token matching | #6 matched context-length |
| curriculum drift | frozen held-out generation | — (new; add as #10) |

## §6b — First concrete paired analysis unit: `am-state-inversion-canary-s42-4k`

`[OBSERVED]` Certified on `feat/action-memory-controls-v1`, now at **`95bb3a98bcc843e1851d8abd607c3f59180718f5`** (B1–B5 resolution over `350ce9df`; base `6eebed87`, not an ancestor of `origin/main`). I re-ran the focused suite at the new head: **6 passed**. The B-fix adds 215 lines and removes 72 across the same three files.

**This is the paired unit the §6 design was missing.** Until now `base_task_pair_id` was a key with no instance.

### The unit, as specified

`[OBSERVED]` From `research/roadmap/specs/campaign-action-memory-controls-canary.json` — schema now **`campaign-design-spec/v1`**, changed from `campaign-definition/v2`. `[INFERENCE]` That change is worth more than it looks: a *design spec* is non-executable by type, which is a stronger guarantee than `allow_billable: false` on an executable definition. It removes the failure mode where a control spec is accidentally dispatched. Bounds also tightened — `max_output_tokens: 4000`, `max_total_tokens: 104000`, `max_requests: 20` added to the existing `max_cost_usd: 0.0`.

| Field | Value |
|---|---|
| `pair_id` | **`am-state-inversion-canary-s42-4k`** |
| `seed` | 42 |
| `dose_bytes` | 4096 |
| `contrast_variable` | **`state_inversion_status`** — exactly one |
| arm 0 | `control_non_inverted` · `baseline_clean_non_inverted` · `inversion_count: 0` · `repeats: 1` |
| arm 1 | `treatment_state_inverted` · `stale_value_override_inversion` · `inversion_count: 1` · `repeats: 1` |

`[DERIVED]` This instantiates the Architect's envelope element 1 (`base_task_pair_id`) and element 8 (statistical unit / pair key) concretely, and it holds exactly one contrast variable — the condition my §6 required and Phase A did not satisfy.

### B1–B5 verification at `95bb3a98`

`[OBSERVED]` I checked each claimed mechanism against code, spec and tests rather than accepting the summary.

| Claim | Verified | Where |
|---|---|---|
| **Exact 4096-byte parity on both arms** | **Enforced** | `assert total_bytes_arm0 == total_bytes_arm1 == 4096` in code; per-chunk `c0["byte_count"] == c1["byte_count"]` under `strict=True` zip in tests. Composition $256 + 256 + 3584 = 4096$ |
| **Diff allowlist** | **Enforced, strongest form** | `differing_keys == {…}` — strict **set equality** on exactly 8 keys, not a subset check; chunk 0 and chunks 2–6 asserted byte-identical; chunk 1 differs only in content/id/type at matched 256 bytes |
| **Missing state journal ⇒ unknown verdict (HOLD)** | **Enforced** | 3 code sites, 3 test sites |
| **Canonical contract SHA-256 source digest** | **Enforced** | `source_digest = f"sha256:{hashlib.sha256(contract_bytes).hexdigest()}"`, computed over contract bytes and attached to the emitted fact |
| **Non-executable `campaign-design-spec/v1`** | **Enforced by type** | schema string changed; bounds tightened |
| **Fixture-only scope** | Declared | code, spec and tests |
| **6/6 tests** | **Reproduced** | `6 passed` at `95bb3a98` |

`[DERIVED]` **The byte-parity invariant is the most consequential item.** It discharges my negative control #6 (matched context-length) and the Architect's *token matching* confound separator as an **executable assertion** rather than a design intention. Combined with the strict diff allowlist, this pair now provably varies one thing.

### Two items are recorded, not enforced

`[OBSERVED]` **1. `tool_schema_digest` — RESOLVED at `d9a06a3e` (and in program HEAD `8e02fba2`).** At `95bb3a98` it was `sha256:49aa3b28…`, present in the spec and nowhere else: computed in no code path, asserted in no test. It is now

```
CANARY_TOOL_SCHEMA_DIGEST = compute_tool_inventory_digest(CANARY_TOOL_INVENTORY)
```

derived by `hashlib.sha256` over the serialised inventory and asserted three times. 6/6 pass.

`[DERIVED]` **The spec value changed from `sha256:49aa3b28…` to `sha256:607ccc5e…`** — so the declared digest was not merely unenforced, it was **wrong**, and adding the assertion is what surfaced it. `[INFERENCE]` That is the case for enforcement over declaration in one data point: a declared digest nobody checks will drift, and nothing will notice.

`[OBSERVED]` **2. `load_trial_bundle` is used 0 times.** The tests build the bundle directly via `BenchmarkContractRecord(...)` + `TrialBundle(...)` in `_build_bundle_from_synthesized`. `[DERIVED]` So two different claims must be kept apart:

| Claim | Supported? |
|---|---|
| The producers can consume a well-formed bundle | **Yes** — newly demonstrated end-to-end |
| A promoted trial directory can be loaded | **No** — still **0 / 170**, unchanged |

`[INFERENCE]` This is real progress on G2 and none on G1. Constructing a bundle in-process proves the consumer contract; it does not exercise the promotion path, which is where the missing `benchmark_contract.json` lives.

### It is a control. It is not behaviour.

`[OBSERVED]` `execution_policy`: `allow_billable: false`, `max_cost_usd: 0.0`, `require_human_approval: true`, `max_concurrent_trials: 1`, `max_transient_retries: 0`. `status: designed`. Zero model runs.

`[INFERENCE]` **Class D, promoting to class C once executed with oracle/nop.** It certifies the instrument and the pair structure. It contributes **nothing** to any behaviour numerator or denominator, and the oracle/nop arms must never appear in a capability rate — the same rule the brief already applies to the MCP oracle `10/10` and blind-retry `0/10`.

### What the six tests certify

`[OBSERVED]`

| Test | Certifies |
|---|---|
| `…freezes_exact_single_contrast` | context budget, tool schema, seed, entity held constant; only inversion varies |
| `…oracle_controls_pass_and_extract_correct_features` | oracle succeeds on **both** arms with binding match and complete handle coverage |
| `…nop_and_stale_mutant_controls_fail_closed` | nop and stale-value mutants fail closed with deterministic failure states |
| `…state_journal_absence_classified_as_observability_failure` | **journal absence ⇒ observability failure / HOLD, not task failure and not "zero state change"** |
| `…canonical_paired_condition_facts_emission` | `PairedConditionFact` rows for both arms with lineage, condition, state diff |
| `…campaign_spec_validity_and_zero_billable_guard` | spec bounded to 1 pair / 1 repeat and strictly non-billable |

`[INFERENCE]` The fourth test is the most valuable thing here and it generalises beyond action-memory: **it is refusal-not-zero applied to observability.** A missing state journal is an instrument gap, not evidence that state did not change. That is precisely ADR-020's *state-certified replay* prerequisite, discharged as an executable invariant rather than a policy sentence.

### `PairedConditionFact` now has its first producer

`[OBSERVED]` `emit_canary_paired_condition_fact` in `control_canary.py` constructs `PairedConditionFact`. `[DERIVED]` When I audited the semantic-fact layer, `PairedConditionFact` was a schema with **zero producers**. This is its first. `[INFERENCE]` One gap: `provenance_kind` is not asserted anywhere in the test file. For a fact emitted from synthesized control artifacts the correct value is `mechanical` — it must not be able to pass as `benchmark_verifier`, or a control fact becomes indistinguishable from a verified outcome downstream.

### What it does not do — stated so nobody over-reads the certification

`[DERIVED]` The canary does **not** close the contract-artifact gap: promoted trials remain **0 / 170** loadable, and `load_trial_bundle` is still exercised by nothing outside `tests/`. Two separate problems, and this fixes the pair-structure one.

`[INFERENCE]` A framing difference worth recording: I listed state inversion as **negative control #3** (*"inverting which write is latest does not change the bound value → the model is using position, not state"*). The canary uses it as the **treatment** arm. Both are valid and they answer different questions — as treatment it asks whether the agent tracks state; as control it asks whether the metric is position-sensitive. **Keep both.** Once a real arm runs, the control reading is what protects the treatment reading from being a positional artifact.

### Approved scope: fixture-only, non-behavioural, with a named exit

`[OBSERVED]` Per the qualification lane, branch head `d9a06a3e` is approved for **fixture-only integration**, and action-memory **remains fixture-only** in program HEAD `8e02fba2`. Its paired design and fixtures are non-behavioural until **B5 evidence** exists, and must not be used as a model result.

`[DERIVED]` I checked all four B5 conditions. All four are open:

| B5 requirement | At this head | Why |
|---|---|---|
| Registered package | **No** | has not traversed `task_workbench → TaskCertificationEnvelope → registry.promote_task` |
| oracle/nop **results** | **No** | that behaviour is *asserted in tests*; no promoted run artifacts |
| CAS references | **No** | `load_trial_bundle` still used 0 times |
| Outcome evidence | **No** | `execution.model_runs = 0` |

`[INFERENCE]` So fixture-only is the **correct** disposition, not a conservative one. The canary stays paired unit #1 for *design* purposes — it supplies `base_task_pair_id`, the single-contrast guarantee, and the byte-parity and tool-schema invariants — and supplies **no** measurement.

### Power: by construction this yields no comparison

`[DERIVED]` `repeats: 1` on each arm ⇒ **one paired unit**. The exact two-sided sign test on $d$ discordant units has floor $2/2^{d}$, so at $d = 1$ the floor is $1.0$.

$$d = 1 \;\Rightarrow\; p_{\min} = 2/2^{1} = 1.0$$

`[INFERENCE]` **No possible outcome of this canary can support a comparative claim**, and that is correct — a canary's job is to prove the instrument emits resolvable, paired, single-contrast facts. Recording the floor explicitly is what stops the first real run from being reported as a finding. Per §6 sizing, the memory main effect needs on the order of 36 independent seeds and the interaction roughly 4× that; this pair is unit 1 of that ladder.

### Where it lands in the gates

| Gate | Effect |
|---|---|
| **G0 — Envelope** | Advances element 1 (pair identity) and element 8 (unit/pair key) from key-without-instance to instantiated |
| **G1 — Ingestible** | **Unchanged.** Still 0/170; `load_trial_bundle` untouched |
| **G2 — Facts resolve** | **Materially advanced at `95bb3a98`** — producers now demonstrated end-to-end on a well-formed bundle with a canonical contract digest; memory's `ContextOperationFact` extractor still absent (§3) |
| **G4 — Controls discriminate** | **Advanced further at `95bb3a98`.** oracle passes both arms, nop and stale mutants fail closed, and byte parity plus the strict diff allowlist now make the single-contrast claim checkable rather than asserted |
| **G6 — Powered** | Explicitly not met, by design; floor $= 1.0$ at one unit |

`[INFERENCE]` **Recommended disposition:** adopt `am-state-inversion-canary-s42-4k` as paired unit #1 of the action-memory ladder; execute the oracle/nop arms to move it class D → C; keep it out of every behaviour rate; and do **not** treat its certification as evidence about any model.

### One unreconciled detail

`[OBSERVED]` The per-file digests reported at `350ce9df` — `ddc5b4b2`, `0f03fa11`, `722ecc1d`, verifier `sha256:b134c7e7…` — matched neither `git hash-object` nor `sha256sum` of file contents, and none appeared in the repository. `[INFERENCE]` Most likely a producing-tool scheme rather than a mismatch; both commit SHAs matched exactly and both suites reproduced. `[INFERENCE]` The B-fix's **canonical contract SHA-256** now gives one digest that *is* reproducible from the tree, which is the right direction — extending the same treatment to `tool_schema_digest` would close the remaining gap.

---

## §6c — Calibration-only constraint: Darwin isolation is not enforceable

`[OBSERVED]` From the qualification lane (wH:p9) at `d1b8a486`: qualification is complete but **Darwin hostname allowlisting is not effective.** All resulting runs are **calibration-only**; causal and network-isolation promotion are refused. Synthetic integrated at `229b3fd6`; the 2×2 remains `dependency_hold` (§6).

`[OBSERVED]` I verified this against the mechanism rather than the report. `src/evallab/harbor_network.py` already computes it correctly and honestly:

> *"Harbor's Docker provider enforces `no-network` and `allowlist` only on Linux"*

```
if system == "Darwin":
    network_isolation_enforced=False,
    network_isolation_reason="darwin-docker-cannot-enforce-no-network",
...
allowlist_enforced = bool(agent_allowed_hosts and host.network_isolation_enforced)
```

`[DERIVED]` So on Darwin `allowlist_enforced` evaluates to `False` by construction, and the module records an explicit *allowlist-to-public adaptation* for unsupported hosts. **This is not a defect — it is a correctly-reported platform limit that fails honest rather than fails silent.** It also independently confirms the E2 lane-certification argument: Darwin→Linux is not a one-variable change, and network mode is one of the factors that moves with it.

### The hazard my own §2 unblock created

`[DERIVED]` I measured where the constraint is actually carried:

| Level | Carries `evidence_class` / `calibration_only` |
|---|---:|
| Top-level summaries | **5 files** |
| Job directories | 0 |
| **Trial directories** | **0 / 170** |

`[DERIVED]` And of the **128** trials made loadable by `fix/promote-benchmark-contract` @ `25e15812`: platform marker present in **0**, `network_isolation_enforced` present in **0**.

`[INFERENCE]` Before the contract backfill this gap was **inert** — nothing could run the governed producers over these trials, so nothing could mis-read them. Now 128 load, the 240-feature registry can consume them, and **no trial artifact says the evidence is calibration-only.** Someone will compute a capability rate off them and be wrong. **Making evidence analysable without carrying its admissibility forward is a regression in epistemics even though it is progress in plumbing**, and I introduced it.

### Hard gate (adopted)

`[INFERENCE]` Applies to every analysis over the newly-loadable trials, without exception:

| Rule | |
|---|---|
| **G7 — Admissibility must travel with the trial** | An analysis over a trial lacking a resolved `evidence_class` and isolation status **refuses**. Absent provenance is not permission. |
| No causal claims | Isolation is unenforced; egress cannot be excluded as a mechanism |
| No network-isolation promotion | `allowlist_enforced=False` by construction on Darwin |
| No capability rate | Calibration-only, per the qualification lane |
| `absent ≠ false` | An absent isolation flag must not be defaulted to `false` any more than to `true` — they are different claims |

### Resolved at `d709cf6d` — provenance now travels with the trial

`[OBSERVED]` Fixed in the contract derivation, which was the right home. Independent recount:

| | Before | After `d709cf6d` |
|---|---:|---:|
| Promoted trial dirs | 170 | 170 |
| With contract | 130 | **130** |
| Loadable | 128 | **128** |
| Contracts carrying all 5 provenance fields | 0 | **130 / 130** |

`[OBSERVED]` Every contract now carries `host_platform=Darwin`, `network_isolation_enforced=False`, `network_isolation_reason=darwin-docker-cannot-enforce-no-network`, `allowlist_enforced=False`, `evidence_class=calibration_only_darwin_public_egress`. Unresolvable isolation fails closed. 9 focused tests pass.

`[DERIVED]` **Counts did not drop.** I predicted they might; that was a hedge, not a finding — all 130 are Darwin/calibration-only, so none hit the fail-closed branch. `[INFERENCE]` G7 is now enforceable from the trial artifact alone, which is what makes the 128 safe to analyse.

---

## §6d — Program HEAD `8e02fba2`: LoCoMo blocked at the digest layer; candidate ≠ evidence

`[OBSERVED]` Program HEAD `8e02fba2` integrates the action-memory fixture stack plus synthetic and platform work — it contains `229b3fd6` (synthetic) and `d1b8a486` (qualification). It carries the same `tool_schema_digest` assertion as `d9a06a3e` but is **not** a descendant of it, so the two are rebased variants of the same change.

`[OBSERVED]` **LoCoMo remains 0 paths at `8e02fba2`**, consistent with §1.5. Its promotion is now blocked on an architecture question: **upstream source digest versus certified hardened package digest.**

`[OBSERVED]` That question is visible in `registry.py` (2,112 lines), which maintains **two digest namespaces** and reconciles them in a binding check:

```
binding.get("candidate_package_digest") != candidate_digests.get("package")
or binding.get("package_digest")       != candidate_digests.get("registry_package")
```

`[INFERENCE]` So a task has a *candidate* package identity and a *registry* package identity, and promotion requires them to bind. For an imported upstream benchmark like LoCoMo the upstream source digest and the digest of the locally hardened package are **not the same object**, and deciding which one a claim cites is a genuine architecture decision, not a bug. Until it is settled, LoCoMo cannot promote — which means the memory lane's first real canary is blocked one layer below the extractor gap in §3.

### `candidate` registry state is not evidence

`[OBSERVED]` `registry.py` exposes four status literals: `candidate`, `pending`, `certified`, `registered`.

`[INFERENCE]` Per the qualification lane, **candidate registry state must not be treated as executable or behavioural evidence.** Added to §1.2 as a distinct admissibility class:

| Registry state | Licenses |
|---|---|
| `candidate` | **nothing** — a proposal, not a package |
| `pending` | nothing; awaiting certification |
| `certified` | task quality only — *not* agent capability |
| `registered` | execution admissibility, still subject to G7 provenance |

`[INFERENCE]` The trap is that `candidate` rows are queryable and look like inventory. A count of candidates is not a count of runnable tasks, and must never appear in a denominator.

### The through-line: every blocker in this program is an identity/digest problem

`[DERIVED]` Five separate blockers, one root-cause class:

| Blocker | Reduces to |
|---|---|
| 2×2 `dependency_hold` (§6) | `tool_schema_digest` + stable action-memory task identity |
| 0/170 unloadable (§2) | missing `benchmark_contract.json` — a per-trial identity record |
| Admissibility hazard (§6c) | provenance not carried into that identity record |
| Action-memory fixture-only (§6b) | no registered package, no CAS references |
| **LoCoMo promotion (§6d)** | upstream source digest vs certified hardened package digest |

`[INFERENCE]` **None of these is an experiment-design problem and none is a data-volume problem.** They are all the same question — *what exactly is this artifact, and does its identity bind to what I am claiming about it* — appearing at five different layers. That is worth stating plainly because it changes the priority: a single coherent digest/identity architecture would unblock all five, whereas fixing them one at a time will keep producing the pattern where each unblock creates the next hazard (as §6c did).

---

## §7 — Leakage, denominators, small-sample limits

| Risk | Specific to this program | Guard |
|---|---|---|
| **Outcome-defining predictors** | `certified_recovered_faults`, `autonomous_recovery_rate`, `stale_value_override_rate`, `conflict_resolution_success`, `temporal_consistency_rate` are all `verdict_coupling="defines"` | `audit_predictor_eligibility` must gate every model; 19 `REWARD_DEFINITION_LEAKAGE` refusals already fire |
| **Zero-denominator collapse** | 6 of the metrics in §4 are rates over opportunity counts | null on zero denominator, never 0 |
| **Pseudo-replication** | repeats reuse task content; Phase A's 18 "pairs" were 9 independent units | declare **statistical unit = independent seed** and **cluster key = `base_task_pair_id`** in `CampaignAnalysisSpecV1` before the run (envelope element 8) |
| **Control contamination** | oracle `10/10` and blind `0/10` are instrument checks | never enter a model denominator; label class C in every table |
| **Regenerated-summary drift** | §1.4 — the summary generator is not in the repo | regenerate from raw trials with committed code before citing again |
| **Missing-contract silent exclusion** | §2 — 170 trials fail to load | make the failure *counted*, not just raised, so exclusions are visible |
| **Treatment-correlated capture loss** | Phase A's 2 ATIF discordances were both `64k semantic_distractor`: 2/6 vs 0/30, **Fisher $p = 0.0238$** | **Mandatory pre-claim test**, see below |

### Treatment-correlated capture-loss check (mandatory, pre-claim)

`[INFERENCE]` Run this **before** any arm comparison, on every campaign, as a gate rather than a diagnostic:

1. Build the $2 \times k$ table of `capture_ok` × condition, where `capture_ok` requires ATIF/event-order concordance **and** envelope element 4 satisfied.
2. Fisher exact (or Barnard for larger tables). **Refuse the arm claim if $p < 0.05$.**
3. Report the table regardless of outcome — a null result is evidence the instrument is condition-independent and belongs in the record.
4. If it fires, the arm effect is **unmeasurable**, not small: the instrument degraded in the arm under test.

`[DERIVED]` Worked example from Phase A: capture loss 2/6 in `64k semantic_distractor` versus 0/30 elsewhere, $p = 0.0238$. Under this gate the Phase-A arm claim is **refused**, which is the correct disposition and was not the original one.

### Explicit denominator register

`[INFERENCE]` Every rate in §4 declares its denominator, its zero-behaviour, and whether the denominator is **gold** (authored) or **inferred**. ADR-020 requires gold opportunity dependencies, so the last column is a gate, not metadata.

| Rate | Denominator | On zero | Gold or inferred |
|---|---|---|---|
| `write_read_link_rate` | `memory_write_count` | null | inferred from facts |
| `read_use_link_rate` | `write_read_link_count` | null | inferred from facts |
| `end_to_end_use_rate` | `memory_write_count` | null | inferred from facts |
| `boundary_carryover_rate` | `boundary_carryover_opportunity_count` | null | **must be gold** — authored boundary opportunities |
| exposure-conditioned recovery | `fault_detected_count` | null | inferred (detection) over **gold** `injected_fault_count` |
| injected-conditioned recovery | `injected_fault_count` | null | **gold** — `FaultInjectionRecord` |
| `designated_move_rate` | post-exposure tool calls | null | inferred |
| `failed_prefix_share` | `total_prompt_tokens` | null | mechanical |
| `stale_binding_survival_rate` | `stale_read_count` | null | inferred |

`[OBSERVED]` The Architect states it independently: *"fault opportunity is defined only by a valid `FaultInjectionRecord`."* `[INFERENCE]` The same rule must apply to memory: **carryover opportunity is defined only by an authored boundary**, never by observing that a boundary seems to have occurred. Otherwise the denominator is a function of agent behaviour and the rate is not interpretable.

**When results stay descriptive.** `[INFERENCE]` Any of the following forces descriptive-only reporting: fewer than 6 discordant paired units (the exact sign test cannot reach $p<0.05$ below that); a single outcome class; zero-variance cells; unvalidated score-scale binding across axes; or a summary whose generator is not committed. All five are already encoded as refusal codes — use them rather than inventing prose caveats.

---

## §8 — SPADE: the correction, verified as far as the primary source allows

`[OBSERVED]` Read the arXiv abstract for **2608.19197**, *SPADE: Self-Play in Adaptive Synthetic Executable Environments* (Liu et al., 2026-08-19). It corroborates the brief's correction on the decisive point:

> *"…grounding the Environment Designer on documents sampled from a large pretraining corpus, and giving **it** an accumulated environment memory."*

`[DERIVED]` The memory component belongs to the **Environment Designer**, not the executing Reasoning Agent — exactly the brief's reading. The abstract reports tool-use gains as a separate setting: **+5.7 on BFCL-v4 multi-turn** and **+13.9 on ACEBench-Agent**, alongside **+5.3 average** on eight held-out reasoning benchmarks.

`[INFERENCE]` **Accept the brief's correction and its conclusion**: "memory improves tool use" is a hypothesis, not a SPADE result, because the memory ablation and the tool-use experiment are on different components and different task sets.

**One boundary on my own verification.** `[OBSERVED]` I verified from the abstract only. The brief's specific figures — Table 3's games-domain suite average falling **58.3 → 53.2**, and tau2-bench appearing in Table 2 — are **not in the abstract** and I did not fetch the full text. The abstract names BFCL-v4 and ACEBench-Agent with numbers but not tau2-bench. `[INFERENCE]` Treat those two specifics as brief-reported pending a full-text check; they do not affect the correction, which the abstract independently supports.

---

## §9 — First-canary analysis recipe

`[INFERENCE]` The brief asks what to run "once one real LoCoMo ATIF canary exists." Given §1.5, LoCoMo does not exist, so I give the recipe in trigger form — it applies to the first canary of **any** memory task.

**Preconditions, all mandatory:**

1. The trial promotes `benchmark_contract.json`, `benchmark-events.jsonl`, `final-state.json`, and `load_trial_bundle` succeeds. (§2)
2. An ATIF → `ContextOperationFact` extractor exists and emits `memory_write` / `memory_read` / `memory_use` with `step_index` and `content_digest`. (§3)
3. The analysis spec declares estimand, unit of analysis, denominators, and headline binding **before** the run.

**Then, in order — none of these is a comparison:**

| # | Analysis | Output |
|---|---|---|
| 1 | Capture completeness | fraction of steps with resolvable operations; refuse below a declared floor |
| 2 | Fact-yield audit | counts of write / read / use; if `memory_use_count = 0`, stop — the task has no use opportunity |
| 3 | Linkage integrity | `write_read_link_rate`, `read_use_link_rate` with `link_basis="content_digest"` |
| 4 | Boundary opportunity | `context_boundary_count` and `boundary_carryover_opportunity_count`; null the rate if opportunity is 0 |
| 5 | Missingness vs condition | is capture loss correlated with the arm? (the Phase-A lesson) |
| 6 | Descriptive report | rates with Wilson intervals, no test, no comparison |

`[INFERENCE]` **A single canary licenses no comparison at all.** Its job is to prove the instrument produces resolvable facts and to size the next experiment. Any arm contrast from $n=1$ is a category error.

---

## §10 — Stop/go for expanding to a repeated anchor set

| Gate | Threshold | If it fails |
|---|---|---|
| **G0 — Envelope** | Elements 1–6 of the minimum comparable-trial envelope present; element 2 requires a declared profile | Architect P0/P1; do not dispatch |
| **G1 — Ingestible** | `load_trial_bundle` succeeds on 100% of promoted canary trials (envelope element 4) | Fix promotion; do not proceed |
| **G2 — Facts resolve** | ≥95% of steps yield operations with `step_index` and `content_digest`; `memory_use_count > 0` | Fix the extractor or the task; the task may have no use opportunity |
| **G3 — Reproducible** | Summary regenerates byte-identically from committed code | Commit the generator before citing any number |
| **G4 — Controls discriminate** | Read-without-use and matched-context-length controls separate from the treatment arm. **Partly discharged for action-memory by the canary (§6b): oracle passes both arms, nop and stale mutants fail closed** | The metric measures retrieval or length, not memory |
| **G5 — Capture loss condition-independent** | Fisher exact on `capture_ok` × condition gives $p > 0.05$; table reported either way | **Arm claim refused**, not merely caveated — the instrument degraded in the arm under test |
| **G6 — Powered** | ≥6 discordant paired units projected; MDE and minimum informative paired units predeclared in `CampaignAnalysisSpecV1`; units = independent seeds clustered by `base_task_pair_id` | Refuse inference; descriptive only; do not run the interaction |

`[INFERENCE]` **G1–G3 are free** — no model calls, no new tasks. They are repair and plumbing. G4–G6 need task variants and trials.

### These gates are not new — ADR-020 already approved most of them

`[OBSERVED]` `research/analysis/automated-trajectory-overnight-ledger.md` records **ADR-020, approved**: *"DeepPlanning, LOCA, Recovery, and Memory remain HOLD — oracle isolation, neutral matched padding, state-certified replay, T>=3, and gold opportunity dependencies are prerequisites."*

`[DERIVED]` My gates map onto it nearly one-to-one, and the ADR names two I had missed:

| ADR-020 prerequisite | My gate | Note |
|---|---|---|
| state-certified replay | G1, G2 | same requirement, stated mechanically |
| neutral matched padding | G4 / control #6 | partly built — Phase A already has the axis |
| T ≥ 3 | G6 | ADR gives a floor; G6 gives the discriminating quantity (≥6 discordant units) |
| **oracle isolation** | — | **I missed this.** The memory analogue: the target must not be readable from the environment |
| **gold opportunity denominators** | §4 | ADR requires *authored* opportunity counts, not inferred — stronger than "declare the denominator" |

`[INFERENCE]` **Oracle isolation deserves to be control #9.** If the target value is recoverable from the environment, every memory metric measures reading, not remembering — and that belongs in the task contract, not the analysis.

---

## §11 — Interesting observations

1. `[DERIVED]` **164 real trials already exist and none are governed.** The program's binding constraint is a promotion contract missing one JSON file, not model access. That is a rare case where the cheapest fix has the largest effect.
2. `[OBSERVED]` **The memory and recovery lanes are blocked differently** — recovery lacks an artifact, memory lacks an extractor. Treating them as one "we need trajectories" problem would fix neither.
3. `[OBSERVED]` **`ALTERNATIVE_REPAIR_MOVES` already exists**, so the wrong-repair negative control (#7) is constructible today with no new benchmark design. The brief's control list omitted the one control the codebase already supports.
4. `[OBSERVED]` **The two persistence levels are the sharpest existing design feature.** `build_recovery_persistence_curve` over `CAMPAIGN0_PERSISTENCE = (1, 2)` distinguishes *recovers once* from *recovers under repetition*, which is exactly the valid-backoff-vs-stagnant-retry falsification the wider program needs.
5. `[OBSERVED]` **3 of 8 planned control views exist** — `v_composite_outcome_validity`, `v_reward_authority`, `v_predictor_eligibility`. The brief's "eight control views" overstates by five at this baseline; there are 76 views in `sql/` overall, and `behavior.sql` contributes 6 behavioural ones not in the planned eight.
6. `[OBSERVED]` **The Architect's synthetic-certification risk is closed.** The review flagged that `SyntheticCertificate.is_passing` permitted `mutants_tested_count == 0`. At `e2a73313` it now requires `mutants_tested_count >= 3` **and** `mutants_failed_count == mutants_tested_count`, with `MIN_MUTANTS_REQUIRED = 3` and `ORACLE_RUNS_REQUIRED = 3`; records with 0, 1 or 2 mutants remain nonpassing. `[INFERENCE]` A vacuous certificate can no longer qualify a task for measurement promotion, and `canonical_admission.synthetic_certificate_is_passing_direct_measurement_consumer: false` closes the same door from the consumer side.
7. `[INFERENCE]` **The SPADE correction generalises into a house rule.** SPADE's error mode — attributing a gain from component X's ablation to component Y's benchmark — is the same shape as using an outcome-defining feature as a predictor. Both are claims that cross a boundary the design never tested. `verdict_coupling` is the mechanical guard for the second; the first needs a citation-level guard, and this brief's correction is the first instance of one.

---

## Verification note

`[OBSERVED]` Executed at `67b4051a` in an isolated worktree (`/tmp/analyst-mtt`, branch `analyst/memory-tool-trajectory-program`): job-lane enumeration over 24 `result.json` rollups; artifact census over 170 promoted trial dirs; `load_trial_bundle` executed on 3 named trials and on all 170; caller topology for `load_trial_bundle` and 4 extractors; AST field extraction for `MemoryContinuityFeatures` and `McpRecoveryFeatures`; `ContextOperationFact` construction sites; `DESIGNATED_REPAIR_MOVES` / `ALTERNATIVE_REPAIR_MOVES` / `CAMPAIGN0_PERSISTENCE`; view inventory across 13 `sql/` files; LoCoMo path search across worktree, `HEAD`, `origin/main`, and all-ref history; SPADE arXiv abstract.

`[DERIVED]` Computed here: the admissibility classification; the $5\times2+10=20$ cell reconciliation; the 0/170 load result; the summary-reproducibility test across code vs data files; MDE figures from the repository's own estimator.

**No production code edited. No test suite run. No model runs launched. No credentials touched.** The user's main checkout was not modified; all work was done in a separate worktree.

**Three claims in the brief I could not confirm, stated as open rather than accepted:**

| Brief claim | Status |
|---|---|
| LoCoMo conv 26, 199 questions, oracle 1.0 / nop 0.0 | **Unverifiable** — LoCoMo absent from all refs |
| Oracle exposure-conditioned recovery 10/10, blind retry 0/10 | **Not located as promoted evidence.** The contract and `fixed_policy.py` support producing it; I found no promoted artifact carrying those results |
| "Eight control views" implemented | **3 of 8** present at this baseline |

`[INFERENCE]` None of these weakens the program; two of them strengthen the §10 gating argument, because they are exactly the kind of claim that G3 (reproducible from committed code) exists to catch.
