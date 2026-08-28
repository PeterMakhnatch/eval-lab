---
type: engine-spec
topic: capability-curve-engine
author: analyst
date: 2026-08-27
status: distilled
epistemic: design + evidence-grounded substrate audit
collection: trajectory-analysis
reviewed: 2026-08-27
requested_by: Research-Eval Capabilities (wH:p9)
depends_on:
  - research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-RESEARCH-PROGRAM-2026-08-27.md
  - research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md
constraints: no code in this artifact; no LLM-judged labels; no implementation overlap with in-flight PR repairs
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-27
license_note: Internal research synthesis; Eval Lab repository license applies.
feeds:
  - parked
---

# Capability-Curve Engine — Specification

Converts a capability hypothesis into a **calibrated threshold coordinate with
uncertainty**, or refuses to produce one. Replaces "model X scores Y" with
"model X degrades past dose D, with slope S, on construct C."

Requested by Research-Eval with seven named deliverables; each is addressed in
its own section and cross-referenced in §12.

---

## 1. Why this and not another benchmark

**Scores decay; coordinates compound.** A score is invalidated by saturation,
contamination, and model turnover. A coordinate — "degrades past ~12 concurrent
tool schemas" — survives model churn and is directly comparable across
generations. METR's time-horizon number is cited constantly for exactly this
reason.

**Prior-art verdict: open niche.** Established independently by two literature
surveys this session:

| Prior art | Laddered factor | Fit | Headline | Gap |
|---|---|---|---|---|
| METR time horizons | human task duration $T$ | logistic on $\log_{10} T$ | $T_{50}$, $T_{80}$; ~7-month doubling | factor is **extrinsic** and non-mechanistic; METR itself flags duration as a loose proxy and monotonicity as an *assumption* |
| IRT for LLMs (CRFM, Princeton) | item difficulty $b_i$ | 2PL/3PL | latent ability $\theta$ | assumes **unidimensionality**, which breaks on multi-step agents with compound failure modes |
| NIAH / RULER / BABILong | context length × needle depth | heatmaps, stepwise thresholds | effective context window | passive text-in-text-out; **no tool execution, no state mutation** |
| FuncBenchGen (ICLR 2026) | DAG depth, node count, distractor ratio | discrete tiers | tier accuracy tables | reports **discrete percentages**, no parametric fit, no CI, no coordinate |
| GA-Rollback / AgentRx ([arXiv:2503.02519](https://arxiv.org/abs/2503.02519)) | step index $k$ | empirical $P(\text{recover} \mid \text{error at } k)$ | irreversible-failure point $k^*$ | used as an **architectural mitigation**, never as an eval calibration metric |

Unclaimed as an end-to-end framework: laddering **mechanistic** control factors,
fitting parametric dose-response with bootstrap CIs, emitting a coordinate vector
$\mathbf{C} = \langle c_{\text{context}}, c_{\text{depth}}, c_{\text{distractor}}, c_{\text{fault}} \rangle$,
and localizing $k^*$ as a calibrated quantity.

**Our differentiation in one line:** METR owns *duration*; we take *mechanism*.

**Discipline note.** Per Research-Eval, "long horizon amplifies features" is a
**falsifiable intervention thesis, not established fact.** External literature
corroborates it (frontier models 70–90% on 10–30-step tasks, variance compressed;
distinct failure modes emerging above ~50 steps / ~100k tokens) but it is
**unverified in our environment** and is therefore tested by an arm (§10), not
assumed.

---

## 2. Substrate reality check — read this before planning any run

Audited on disk, 2026-08-27. This is the binding constraint on everything below.

| Asset | State |
|---|---|
| Long-horizon runs | **none.** Max successful trial 35 steps; longest wall-clock 4m48s. Only runs >50 steps are 4 failed Gemini loops (56–104 steps, reward 0) |
| Open-model runs | **none.** All executions closed-model (`gpt-5.6-luna`, `gpt-5.6-terra`, `gemini-3.7-flash`) |
| LOCA dose ladder | **absent.** `state.py:14-18` defines 8k/64k/128k; `materializer.py:16-17` hardcodes `loca-abtesting-8k-seed42`. One point, not a curve |
| FuncDAG ladder | 3 materialized tiers, 3 trials, **3/3 pass** → ceiling effect, zero information. Tiers also move depth+width+distractors **together** |
| AgentAbstain | **0 certified pairs admitted**; 130 pending audit |
| `ContextPressureInjector`, `ToolFaultInjector` | implemented, **unused in production** (unit tests only) |
| Static task mass | 444 tasks, but dominated by AIME/GPQA/HumanEvalFix — non-agentic, non-laddered |

**Consequences.**
1. No curve is fittable from data on disk today. Not one.
2. The existing FuncDAG "ladder" is simultaneously **saturated** and **confounded** — it is the textbook error this engine exists to prevent.
3. Block J (open-model telemetry) has **no substrate at all**, so reasoning-token
   elasticity — ranked first in the research program — is currently unrunnable
   and drops in priority (§10).
4. The 4 failed Gemini loops are our only long-horizon-shaped data, and all four
   are derailments. A tiny but real $k^*$ pilot sample.

---

## 3. Object model

### 3.1 `CapabilityFactor` — the dose axis (deliverable: factor/dose contract)

| Field | Meaning |
|---|---|
| `factor_id`, `version` | identity; version bumps on any semantic change |
| `construct` | which capability this factor stresses |
| `dose_scale` | units — tokens, DAG depth, distractor count, fault count, subgoal count, session count |
| `levels` | ordered dose values; **≥ 3 required** (§6), geometric spacing preferred |
| `monotone_direction` | declared expected sign, fixed **before** data collection |
| `single_delta_paths` | the exact task-spec paths permitted to differ between arms |
| `held_constant` | enumerated nuisance dimensions pinned across arms — instruction bytes, tool count, verifier digest, seed set, step budget, token volume where not the factor |
| `confound_arms` | mandatory companion arms varying a nuisance dimension *instead of* the factor |
| `estimand` | which coordinate is the target (§5) |

A factor without populated `held_constant` and `confound_arms` is **not admissible**.
That combination is what separates a curve from an anecdote.

### 3.2 `Arm` — one materialized dose level

Carries `arm_id`, `factor_id`, `level`, and the digest set (`task_digest`,
`verifier_digest`, `environment_digest`, `factor_values_digest`).

Two gates, both blocking:

- **Single-delta proof** (deliverable): the diff between this arm and the base arm,
  restricted to `single_delta_paths`. Any difference outside those paths →
  `ARM_REJECTED_MULTI_DELTA`. Reuses the `SingleDeltaAdmissionGate` pattern.
- **Per-arm certification**: the existing 8-point gate (oracle 3× pass, NOP fail,
  ≥3 mutants fail, clean reset, idempotency, secret isolation) runs **per arm**,
  not once per family. An 8k arm passing certifies nothing about the 128k arm —
  padding can break a verifier, exhaust a window, or alter oracle runtime.

### 3.3 `CurveSpec` and `CurveResult`

`CurveSpec` = factor + arms + model cohort + seed/replication policy + estimand +
stopping rule. `CurveResult` = per-arm counts with denominators, fitted estimand
with CI **or** a refusal reason, monotonicity verdict, confound verdict, coverage
report, and every digest.

---

## 4. Denominators and null-on-zero (deliverable)

Per-arm success denominator counts trials that were **analysis-ready at that arm**:
environment materialized, agent executed, verifier returned a verdict, and the
construct's opportunity was exposed.

Excluded from the denominator, reported separately:

| Excluded | Reason |
|---|---|
| harness crash / infrastructure failure | not a capability observation |
| budget exhaustion before first substantive action | fast-crash denominator bias |
| arm below coverage threshold (§8) | `EVIDENCE_INSUFFICIENT`, not a data point |
| opportunity not exposed | `analysis_ready = null` |

$\lvert\Omega_{\text{arm}}\rvert = 0 \Rightarrow$ that arm's rate is **`null`**,
never $0.0$, never $1.0$. A null arm does not contribute to the fit and counts
against the ≥3-arm requirement.

---

## 5. Estimand (deliverable: monotonicity / threshold estimand)

Functional form deliberately matches METR so results are cross-comparable, with
dose $d$ mechanistic rather than temporal:

$$P(\text{success} \mid d) = \sigma\!\left(\beta_0 + \beta_1 \log d\right)$$

Reported coordinates:

| Coordinate | Definition | Interpretation |
|---|---|---|
| $d_{50}$ | $\exp(-\beta_0/\beta_1)$ | coin-flip dose; comparability anchor |
| $d_{80}$ | $\exp\!\big((\log 4 - \beta_0)/\beta_1\big)$ | **operational** dose — reliability, not coin flips |
| $\beta_1$ | slope on $\log d$ | **diagnosticity.** This is the IRT discrimination analogue: $\beta_1 \approx 0$ means the factor does not discriminate and the ladder is uninformative regardless of accuracy |
| MTD | largest tested $d$ whose success CI lower bound ≥ declared floor | what you can safely ship into |

Log-dose because context volume, DAG depth, and distractor count act
multiplicatively, and because the natural ladders are already geometric
(8k/64k/128k).

**Clustered fit.** Tasks are clusters; seeds are replicates within task × arm.
Random-intercept logistic:

$$P(y_{ijk} = 1) = \sigma\!\left(\beta_0 + u_i + \beta_1 \log d_j\right), \qquad u_i \sim \mathcal{N}(0, \sigma_u^2)$$

$i$ = task, $j$ = dose level, $k$ = seed replicate. Treating trials as i.i.d.
inflates precision severely and is prohibited.

**Paired primary estimator.** Because the same task instances appear at every
dose, the within-task difference $d_i = y_{i,\text{high}} - y_{i,\text{low}}$ is
the variance-reduced primary. Report paired **and** unpaired; material
disagreement means the arms are not truly matched and the curve is suspect.

**No unidimensional ability claim.** Per the IRT caveat, we never fit a latent
$\theta$ across constructs. Coordinates are per (construct × factor × model ×
task-cluster). This is the existing no-universal-score doctrine applied to curves.

---

## 6. Refusal-to-fit (deliverable: refusal when sparse)

The engine emits a refusal, never a best guess. Mirrors `refuse-to-rank`.

| Code | Condition |
|---|---|
| `INSUFFICIENT_ARMS` | fewer than 3 non-null arms. Two points always fit a line and can never test shape |
| `FLOOR_SATURATION` | every arm's success CI overlaps 0 — no information (the IRT floor effect, operationalized) |
| `CEILING_SATURATION` | every arm's success CI overlaps 1. **This is the current FuncDAG state** |
| `MECHANISM_NOT_SUPPORTED` | observed sequence violates `monotone_direction` beyond noise: bootstrap CI on an adjacent-level difference excludes the declared sign. The mechanism is falsified — **no threshold is reported** |
| `CONFOUNDED` | a confound arm's effect CI overlaps the factor arm's effect CI at matched level → factor not isolated |
| `ARM_REJECTED_MULTI_DELTA` | single-delta proof failed on any arm → whole curve invalid |
| `CERTIFICATION_FAILED` | per-arm 8-point gate failed; arm dropped, and if that breaks ≥3 arms, refuse |
| `UNDERPOWERED` | per-arm $n$ below the requirement from the existing power planner |
| `EVIDENCE_INSUFFICIENT` | arm coverage below §8 thresholds |
| `CENSORED_LOW` / `CENSORED_HIGH` | $d_{50}$ falls outside the tested range → report a **bound**, never extrapolate (METR's own >16h caveat is the cautionary precedent) |

`MECHANISM_NOT_SUPPORTED` is the scientific heart of the engine: a
non-monotone ladder is a **refutation of the hypothesized mechanism**, and is a
publishable result in its own right — not a failed run to be quietly retried.

---

## 7. Matched task / seed cluster policy (deliverable)

1. **Same task instances at every dose.** Paired design is the primary variance win.
2. **Seed set fixed per task and replicated across arms.** Seed is a blocking
   factor, not noise to average away.
3. **Bootstrap resamples task clusters**, carrying all arms and seeds together.
   Trial-level bootstrap is prohibited (it fabricates precision).
4. **Cluster-key partition separation**: a task family may not appear in two arms
   under different cluster keys, or the pairing silently breaks.
5. Where an arm loses a task (materialization failure), either drop that task from
   **all** arms or report unpaired only, with the imbalance disclosed.

---

## 8. Minimum ATIF / state-journal coverage (deliverable)

Per arm, all required or the arm is `EVIDENCE_INSUFFICIENT`:

| Requirement | Applies to |
|---|---|
| ATIF validation status valid for ≥ 95% of arm trials | all curves |
| `finish_reason` present | all curves — truncation control; without it, high-dose arms suffer silent survivorship |
| state journal present and non-degraded | any curve whose features need state evidence |
| `is_copied_context` populated | context / compaction curves |
| realized reasoning tokens present | open-model reasoning curves (blocked: no substrate, §2) |
| injected-fault ledger present | recovery curves — supplies the denominator |

---

## 9. Hard prohibitions

1. **Never fit a dose curve to observational $C_0$ rows** across heterogeneous
   tasks. Dose effects require arms. *(Research-Eval, explicit.)*
2. Never pool across benchmarks.
3. Never report $d_{50}$ or $d_{80}$ when monotonicity fails.
4. Never extrapolate beyond the tested dose range.
5. Never average seeds as i.i.d. draws from one condition.
6. Never certify a family from one arm's certification.
7. No LLM-judged labels anywhere in the pipeline.

---

## 10. First curves, re-prioritized against substrate reality

Research-Eval proposed LOCA context, FuncDAG depth×dependency, and recovery fault
dose. I accept all three and reorder by **what is actually feasible now**, and
demote the research program's original #1 because block J has no substrate.

### Curve 1 — FuncDAG composition (FROZEN pending PR #248)

> **Collision ruling (Research-Eval, 2026-08-27):** conceptually no collision,
> **implementation collision YES if started now.** PR #248 on
> `lane/deepseek-v4-flash` is actively changing the shared FuncDAG generator and
> the easy/medium/hard contracts, oracle, and verifier to require **exact
> dependency traces**, after Linux certification found an **answer-only
> exploit**. Those files must not be edited concurrently. This curve is frozen
> against the **post-#248 trace contract** and starts only after #248 merges.

**Analytical consequence of the exploit — my earlier diagnosis was wrong.**
I recorded the current 3/3 pass as `CEILING_SATURATION`. That is now
**unsafe**: if the tasks admitted an answer-only path, the observed passes may be
**exploit artifact rather than capability**, and the verifier — not the model —
was what saturated. The true ceiling is therefore **unknown** until re-measured
under the trace contract. Post-#248 pass rates may fall substantially, and the
useful dose range may begin *below* depth 6 rather than above it. Refusal code on
the pre-#248 data is corrected from `CEILING_SATURATION` to
`EVIDENCE_INSUFFICIENT` (verifier admitted an unintended solution path). This is
a good illustration of why per-arm certification is blocking in §3.2.

**Execution protocol after #248 merges:**
1. Fresh **curve-only worktree**; no edits to the shared generator or to the
   certified easy/medium/hard task identities, which remain byte-stable.
2. Generate **new curve-only task identities** — the ladder never reuses or
   mutates certified tier tasks.
3. **One factor per arm set.** Depth sweep holds constant: **width, connected and
   disconnected distractor counts, operation distribution, and trace schema.**
   Every other factor gets its own separate arm set.
4. Within the distractor factor, still split the three sub-confounds: count with
   distinct names; name-similarity at fixed count; schema token volume at fixed
   count.
5. Re-measure the usable dose window under the trace contract **before** choosing
   ladder levels — do not inherit the pre-#248 assumption that failures begin
   above depth 6.
- **Coordinate:** $d_{50}$ in depth, plus $\beta_1$ per factor to rank which factor
  binds hardest. That ranking is the actionable output.

### Curve 2 — LOCA context dilation and compaction

- **Blocker:** `materializer.py` hardcodes the 8k canary. Materializing 16k/64k/128k
  arms is owned by another lane; PR #247 is a single-condition repair, explicitly
  not a ladder. **I do not implement this** — I specify the contract.
- **Factors:** padding volume as declared in `state.py`; separately, forced
  compaction count $\{0,1,2,4\}$ at fixed volume.
- **Mandatory confound arms:** neutral padding vs semantic-distractor padding at
  matched token volume. Without both, horizon and interference are fused and the
  result is unusable (the LOCA anti-confound arm).
- **Held constant:** step budget. Padding otherwise silently cuts effective budget
  and you measure budget, not context.

### Curve 3 — Recovery fault dose

- **Two axes:** persistence (`fault_count` $\in \{1,2,4,8\}$) and detectability
  (fault class ordered by signal strength: explicit error exit → malformed output →
  **silent wrong result at exit 0**).
- Detectability's dose is *inverse signal strength*, so `monotone_direction` is
  declared accordingly. The silent-wrong cell is the discriminating one: only
  detection can catch it.
- **Denominator:** injected fault opportunities, known by construction.
- **Excludes assisted recovery** via intervention provenance, by design rather than
  post-hoc filtering.
- **Blocked on:** P2 error semantics, and `ToolFaultInjector` reaching a
  materialized task family (currently unit-test only).

### Curve 0 — CLEARED (read-only method validation)

> **Clearance (Research-Eval, 2026-08-27):** approved as **read-only method
> validation** with two binding conditions: **record source and harness
> differences per trace corpus**, and **do not pool benchmark effects.**

**Derailment point $k^*$ on public pre-executed traces.**

Both surveys confirm public per-step traces exist for SWE-bench Verified,
Vending-Bench 1 & 2, tau/tau²-bench, and BrowserGym/AgentLab. Vending-Bench is
the ideal substrate: $k^*$ is objectively measurable as the first turn where the
agent's belief state diverges from environment truth (ledger/inventory mismatch),
after which recovery probability drops below ~5%.

Why this goes first despite being unranked in the original program:

1. **Zero compute, zero new instrumentation** — the traces already exist.
2. It de-risks the $k^*$ method *before* we spend money generating long-horizon
   data of our own.
3. It directly tests the amplifier thesis on real long-horizon data we do not have
   to produce, converting Research-Eval's "treat it as falsifiable" instruction
   into an actual test.
4. Its output is immediately reusable: $k^*$ gates SFT pruning and DPO pair
   selection, per the lab's original mission.
5. It gives the engine a validated second coordinate type ($k^*$ alongside $d_{50}$)
   before any ladder exists.

Method sketch, all $C_0$/$C_1$: define divergence by a deterministic state
predicate supplied per benchmark ($C_1$ contract, e.g. ledger mismatch $> 0$);
locate the earliest step after which no sibling run from the same prefix succeeds;
correlate $k^*$ with foundational features available in those traces (context
occupancy, cascade length, blind-retry rate, thrash). **No prose interpretation.**

**Binding clearance conditions.**

1. **Per-corpus provenance record.** Every trace corpus carries its own
   descriptor: benchmark, version/commit, harness, scaffold, model, sampling
   params where published, verifier semantics, and what the corpus does *not*
   record. Two corpora that both "publish traces" are not interchangeable —
   tau-bench drops `usage`/`logprobs`/`finish_reason` upstream, while
   Vending-Bench publishes state snapshots. $k^*$ computed from a state predicate
   is not the same quantity as $k^*$ computed from a no-progress predicate, and
   the descriptor must say which was used.
2. **No pooled benchmark effects.** $k^*$ distributions are reported **per
   (benchmark × harness × model)**, never merged into a cross-benchmark
   statistic. Extends hard prohibition 2 (§9) from arms to corpora. What may be
   compared across corpora is the **method's behavior** — does the predicate
   locate a $k^*$ at all, is post-$k^*$ recovery uniformly near zero — never the
   effect magnitudes.
3. **Method validation, not capability claims.** Curve 0's deliverable is
   "the $k^*$ predicate is well-defined and locates a real irreversibility
   boundary on long-horizon data," not "model M derails at step N." Any
   capability statement would require our own controlled arms.
4. **Divergence predicate is declared per corpus, in advance** ($C_1$ contract),
   and its false-positive behavior reported: how often the predicate fires on runs
   that subsequently succeed. A predicate that fires before recoverable dips is
   measuring noise, not irreversibility.
5. **Read-only.** No re-running, no model calls, no modification of any corpus.

---

## 11. Outputs

A `CapabilityCoordinate` record, digest-pinned, append-only, supersession-tracked
to match existing verdict patterns:

`construct`, `factor_id` + version, `model`, `task_cluster`, `d50` + CI, `d80` + CI,
`beta1` + CI, `MTD`, `monotonicity_verdict`, `confound_verdict`, `coverage_report`,
`arm_digests`, `refusal_code` (nullable), `engine_version`.

Refusals are first-class records, not absences. A `CEILING_SATURATION` on FuncDAG
is a finding about our task design, and it should be queryable.

---

## 12. Deliverable cross-reference

| Research-Eval request | Section |
|---|---|
| factor / dose contracts | §3.1 |
| arm opportunity denominators, null-on-zero | §4 |
| single-delta proof | §3.2 |
| monotonicity / threshold estimand | §5, §6 (`MECHANISM_NOT_SUPPORTED`) |
| uncertainty / CI with refusal-to-fit when sparse | §5, §6 |
| matched task / seed cluster policy | §7 |
| minimum state-journal / ATIF coverage | §8 |

## 13. Non-overlap and handoffs

- **No implementation.** This artifact is design only; no code, no branch, no PR.
- **No collision with in-flight repairs.** PR #247 (LOCA separate-verifier reward
  reliability) and the trajectory feature stack are untouched. Curve 2 depends on
  a materializer change owned by another lane; I specify the contract and do not
  write it.
- **Tutor:** requesting adversarial review of §5–§7 specifically — the estimand,
  the refusal conditions, and the clustering policy. Prior experience says the
  statistical boundary is where this will be weakest.
- **Architect:** scope approval, lane assignment, and sequencing of Curve 0 (which
  needs no lane, only clearance to analyze public traces).
- **Research-Eval:** confirm Curve 1's escape-the-ceiling depth sweep does not
  collide with FuncDAG workbench certification on `lane/deepseek-v4-flash`.
