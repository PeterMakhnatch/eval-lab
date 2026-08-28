---
type: analyst-reply
topic: focused-benchmark-trajectory-program
author: analyst
date: 2026-08-28
status: distilled
review_state: final-after-tutor
epistemic: recommendation grounded in verified repository state; external asset facts marked by verification tier
collection: trajectory-analysis
reviewed: 2026-08-28
revision: 3 (post-Tutor round 1; three verified repository facts; two P0 wording alignments — Vector B answer-key gate scope, cross-arm rho identity qualification)
responds_to: research/inbox/NEXT-BENCHMARK-TRAJECTORY-PROGRAM-BRIEF-2026-08-28.md
incorporates:
  - research/inbox/NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md
  - research/inbox/TUTOR_CAPABILITY_CURVE_SPEC_ADVERSARIAL_REVIEW_2026-08-27.md
  - research/inbox/TUTOR_REVIEW_OF_ANALYST_PRIMER_2026-08-28.md
constraints: no code; no runs; no LLM-judged labels; no cross-benchmark pooling; the word "capability" is barred (see §0.3)
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-28
license_note: Internal research synthesis; Eval Lab repository license applies.
feeds:
  - parked
---

# Analyst Reply — Focused Benchmark and Trajectory Program (rev 2)

## 0. Reconciliation, scope, and vocabulary

### 0.1 Which draft Tutor reviewed

Tutor's review (`NEXT-BENCHMARK-PROGRAM-TUTOR-REVIEW-2026-08-28.md:20`) records that
`wK:p5` was unreachable at review time, so a **delegated Analyst-role author**
produced a reply at this path, which this document has replaced. Tutor's line
citations (`arXiv:2604.12876` at line 57, $p_0 = 0.40$, "2,376 billable trials",
"20 arms") refer to that draft and do not appear here.

I have therefore **mapped every defect rather than assuming it applied**, and
recorded the disposition explicitly in §9. Seven of twelve applied to my draft in
substance; two did not apply; three applied partially. Tutor's four gating
defects (B1, B2, B5, B6) all applied and are all addressed.

### 0.2 The three verified repository facts that reshape this reply

| # | Verified fact | Consequence |
|---|---|---|
| 1 | LOCA-Lean's materializer **hard-refuses every task except 8k seed42**, and its instruction **mixes large-data aggregation with retention** | LOCA-Lean is confounded *at the instruction level*, not merely unmaterialized. It cannot support any multi-arm cohort. Construct 1 has **no** usable asset, internal or external |
| 2 | `syn-funcdag` tasks expose **files plus a single bash/exec surface, not discrete MCP tools** | Confirmed by inspection: `environment/Dockerfile` copies only `inputs.json`, `dag_spec.json`, `dag_code.py` into `/app/src/`; the agent writes `/app/output/result.json`. **The DAG is traversed inside the agent's own code execution, invisible to ATIF.** Tool-mix, unique-tools, selection entropy, per-call schema rejection, and per-edge conformance **cannot populate** |
| 3 | **No MCP fault interceptor exists** | Construct 3's injection ledger — the denominator-by-construction that justifies it — must be *built*, not configured |

Fact 2 corrected a real error of mine: I had attributed per-edge observability to
the trajectory. It is only in the **output artifact**. Value-propagation accuracy
is Grade A but is an **outcome** measure; the process (which edges were visited,
in what order) is unobservable on the current family.

### 0.3 Scope limits that bar specific words

Per Tutor §3 and confound C5: the brief narrows execution to **one model and one
harness**. Under those conditions:

- The word **"capability" is barred** from all outputs of this program. A
  capability claim requires ≥2 model families **and** ≥2 harness configurations
  (or a published scaffold bound).
- Every result is a scoped statement:
  **(DeepSeek V4 Flash × this harness × this cohort)**.
- **No cross-construct comparison is licensed**, because one unbounded harness
  term is common to all three verticals (C4).

### 0.4 Two orthogonal grade vocabularies

- **Evidence grade (A–D):** A deterministic state/exit/digest · B deterministic
  over text or structure · C calibrated judge · D uncalibrated prose. **A and B
  only are admitted.**
- **Causal grade ($C_0$–$C_3$):** $C_0$ structural · $C_1$ needs a task contract
  · $C_2/C_3$ needs matched single-delta or dose-ladder intervention.

Orthogonal, never fused. Outcome and process evidence are likewise two
orthogonal axes, not a ranking.

### 0.5 External asset verification tiers

Tutor B1 requires that every license/id carry a local `path:line` or be marked
UNVERIFIED with a settling check named. Applied:

| Asset | Tier | Basis | Settling check |
|---|---|---|---|
| FuncBenchGen — BSD-3, **arXiv:2509.26553** | **LOCAL-VERIFIED** | `papers/agentic-capabilities/synthetic/SOURCE-CATALOG.md:37` (per Tutor B1) | — |
| LOCA-bench — architectural **HOLD at `39022d6`** | **LOCAL-VERIFIED** | Tutor substrate audit | — |
| Recovery-Bench — MIT, `harbor[modal]>=0.4.0`, **replay failed 11/20, no StateCertificate** | **LOCAL-VERIFIED** | Tutor substrate audit + upstream `pyproject.toml` | — |
| LOCA-bench MIT + pin `8b6fac49…`; MemGym Apache-2.0 + pin `50b404e6…`; BFCL Apache-2.0 + pin `6ea57973…`; tau2 MIT + pin `a2c02472…` | **AGENT-VERIFIED, NOT LOCAL** | librarian read of upstream repos | clone at pin, read `LICENSE`, record digest in a benchmark card before any adoption |
| arXiv ids for LOCA-bench, MemoryAgentBench, tau2 | **DELETED** | unsourced in my draft | do not reintroduce without a local card |

I deliberately cite **no arXiv id** I have not seen locally.

---

## 1. Portfolio — the program is synthetic-family authoring with one external anchor

Tutor §0 and Peter's facts converge: **selection is not the bottleneck.** Two of
three constructs have no usable external benchmark, and the third's usable family
lacks the tool surface its features require. The brief's own rule licenses this
("prefer a certified synthetic family when external assets are unavailable,
lossy, or confounded").

### Construct 1 — context and actionable memory

| Role | Choice | Status |
|---|---|---|
| **PRIMARY** | **Harbor-native certified dilation/compaction family** (new; built on `ContextPressureInjector`) | must be authored |
| **FALLBACK** | LOCA-bench | **contingent on HOLD at `39022d6` lifting** (neutral-padding anti-confound + sandbox/MCP parity) |
| **REJECTED as unusable** | LOCA-Lean `loca-abtesting-8k-seed42` | materializer hard-refuses all other tasks; **instruction fuses aggregation with retention** |
| **DISQUALIFIED — LLM judge in scoring** | MemoryAgentBench, LongMemEval/V2, BEAM | judge at `longmem_qa_evaluate.py`, `_ABSTENTION_JUDGE_SYSTEM_PROMPT`, `align_with_llm` respectively |
| **DISQUALIFIED — wrong construct** | MemoryAgentBench, BEAM, ContextBench | measure static recall over inert history; actionable memory requires the retained value to be **used as a tool argument** |

**This retracts my rev-1 position** (LOCA-bench primary) and my earlier
compendium's approving citation of BEAM's 100K/1M/10M token buckets. The ladder
is real; the scoring is inadmissible.

### Construct 2 — tool selection, composition, value propagation

| Role | Choice | Status |
|---|---|---|
| **PRIMARY** | **MCP-FuncDAG v2 — explicitly a new family** in which each DAG node is a **discrete MCP tool** | must be authored |
| **external anchor** | FuncBenchGen (BSD-3, arXiv:2509.26553, LOCAL-VERIFIED) | convergent-validity arm |
| **REJECTED as primary** | current `syn-funcdag-{easy,medium,hard}` as-is | single bash/exec surface ⇒ the construct's process features cannot populate (fact 2). Retain as an **artifact-level value-propagation** cohort only |
| fallback | BFCL v4 | deterministic, but static partitions ⇒ no continuous dose axis |
| **DISQUALIFIED — license** | ToolSandbox (Apple custom, patent exclusion), ToolBench-X (non-commercial despite root MIT file) | |

**Why v2 is a new family, not a config change.** Only when each node is a
discrete tool does each DAG edge become an *observable trajectory opportunity*
with its own denominator. On the current family the edges are invisible and the
only measurable is the final artifact.

**Open discrepancy to settle before any medium/hard cell runs (B6).** Verified:
`environment/Dockerfile` copies only the three data files, and `oracle/solve.py`
plus `solution/solve.py` sit in the *task root*, outside the `environment/`
context. Tutor's audit states medium/hard require purging `oracle/solve.py` from
`/app/oracle/` in the build context. These are reconcilable only by inspecting
the actual build invocation's context root. **Treat as unsettled; the
oracle-exclusion gate in §5 is mandatory regardless.**

### Construct 3 — error detection, diagnosis, autonomous recovery

| Role | Choice | Status |
|---|---|---|
| **PRIMARY** | **Harbor-native MCP fault-injection family + interceptor** | **interceptor does not exist — must be built** (fact 3) |
| **realism arm** | Recovery-Bench (MIT, natively Harbor) | **real, runnable repo**; limitation is **replay validity** — replay failed 11/20 with no StateCertificate. Not paper-only |
| fallback | tau2-bench | requires a second model as user simulator, which becomes part of the measurement |
| **DISQUALIFIED** | ToolBench-X (license), AgentRx (LLM judge; diagnostic not harness), AgentCheck (hybrid judge; provenance unverified) | |

Tutor §5 endorses Harbor-native injection as primary "for the right reason: the
denominator is known by construction." That reason survives fact 3 — it simply
costs an interceptor.

---

## 2. Construct → opportunity → L1 → L2 → L3

Feature **orders**: 0 fact · 1 ratio · 2 sequence derivative · 3
conditional/interaction · 4 relational.

### Construct 1 — context and actionable memory

**Opportunity $\Omega$:** a trial in which a value established before a context
boundary is *required as a tool argument* after it. Contract-declared ($C_1$),
counted, never inferred.

| Layer | Item | Order | Grade |
|---|---|---|---|
| L1 | `prompt_tokens`/step; peak occupancy; `is_copied_context` count + positions; `cached_tokens`; `reasoning_tokens`; observation bytes | 0 | A |
| L1 | per-opportunity binding outcome (did the correct value reach the argument) | 0 | A/$C_1$ |
| L1 | **`prompt_cache_hit_rate` per arm** — manipulation check for C1 | 0 | A |
| L1 | **model-call count per arm** — manipulation check for C2 | 0 | A |
| L2 | **binding survival rate** ÷ binding opportunities | 1 | A/$C_1$ |
| L2 | context burn velocity (OLS slope of `prompt_tokens` over step ordinal) | 2 | A |
| L2 | stale-value override rate ÷ conflicting-value opportunities | 1 | A/$C_1$ |
| L3 | neutral-padding ladder; **volume-matched semantic-distractor arm**; needle position; forced compaction | — | $C_2$ |

Absent by design: post-compaction re-read rate — needs state evidence to exclude
legitimately changed files, and construct 1 emits no state journal.

### Construct 2 — tool composition and value propagation

**Opportunity $\Omega$:** on **v2**, each required DAG edge and each required
value binding, as declared by `dependency_trace`. On the **current** family,
$\Omega$ = required bindings **in the output artifact only**.

| Layer | Item | Order | Grade | v2 required? |
|---|---|---|---|---|
| L1 | per-binding exact-value match from `dependency_trace` (artifact) | 0 | **A** | no — works today |
| L1 | per-edge tool invocation, ordering, arguments | 0 | A | **yes** |
| L1 | per-call schema rejection; unregistered-tool calls | 0 | B\* | **yes** |
| L2 | **value-propagation accuracy** ÷ required bindings | 1 | **A**/$C_1$ | no |
| L2 | **DAG conformance** ÷ required edges | 1 | A/$C_1$ | **yes** |
| L2 | tool-selection entropy; redundant-call ratio | 1 | A | **yes** |
| L2 | first-correct-edge latency | 2 | A | **yes** |
| L3 | depth ladder (width, distractors, operation distribution, trace schema held constant); width ladder; distractor surface split three ways — count with distinct names, name-similarity at fixed count, schema token volume at fixed count | — | $C_2$ | partly |

\* Grade **B**: `HARNESS_SCHEMA_REJECTION` is regex over harness text
(`trajectory_error_taxonomy.py` `_SCHEMA_REJECTION_PATTERNS`) — deterministic but
text-dependent.

### Construct 3 — error detection, diagnosis, recovery

**Opportunity $\Omega$:** the **injection ledger** — one row per injected fault,
known before the run.

| Layer | Item | Order | Grade |
|---|---|---|---|
| L1 | injection ledger (class, step, target, payload) | 0 | A |
| L1 | 8-class error category; `InterventionCategory` (autonomous / human-directed / -assisted / -intervened) | 0 | B\* / A |
| L1 | first-error index; exit-code sequence; state diff before/after | 2 / 0 | A |
| L2 | **certified autonomous recovery rate** = invariant restored ∧ goal completed ∧ no human-directed intervention ∧ **paired NOP-retry control failed at the same $k$** ÷ injected faults | 1 | A/$C_3$ |
| L2 | detection rate ÷ injected faults | 1 | A |
| L2 | blind-retry rate ÷ post-fault retries | 1 | A |
| L2 | diagnosis grounding ÷ faults followed by ≥1 mutation | 1 | A |
| L2 | recovery latency (steps/tokens injection → restored invariant) | 2 | A |
| L3 | **persistence ladder** `fault_count ∈ {1,2,4,8}` — ratio scale, **curve permitted** | — | $C_2$ |
| L3 | **class contrast** {permission, not-found, timeout, malformed-output, **silent-wrong**} — ordinal, **curve BARRED** | — | $C_2$ |

The NOP-retry clause closes Tutor D12: on an auto-clearing transient fault, blind
retry satisfies "invariant restored ∧ task passed" *by construction*. Recovery is
credited only when the paired no-adaptation control **fails** at the same $k$.

**Silent-wrong and malformed-output require an out-of-band oracle (D11).** Both
carry exit 0, so detection needs a reference result the agent cannot reach. The
reference is computed **verifier-side and excluded from the agent image**, under
the same gate as B6.

---

## 3. Denominators and grades for every rate

Tutor §5 marks this table as correct and to survive. Every rate is `NULL` at zero
opportunity. Additions from this revision are marked ✚.

| Rate | Denominator | Evidence | Causal |
|---|---|---|---|
| binding survival rate | binding opportunities | A | $C_1$ |
| stale-value override rate | conflicting-value opportunities | A | $C_1$ |
| value-propagation accuracy | required bindings (artifact) | **A** | $C_1$ |
| DAG conformance | required edges | A | $C_1$ |
| tool-selection entropy | tool calls | A | $C_0$ |
| schema conformance | tool invocations | **B** | $C_0$ |
| redundant-call ratio | tool calls | A | $C_0$ |
| certified autonomous recovery rate | **injected faults** | A | $C_3$ |
| detection rate | injected faults | A | $C_2$ |
| blind-retry rate | post-fault retries | A | $C_0$ |
| diagnosis grounding | faults followed by ≥1 mutation | A | $C_0$ |
| tool error rate | tool calls, expected-negative probes excluded | B | $C_0$ |
| ✚ `prompt_cache_hit_rate` per arm | prompt tokens | A | $C_0$ (manipulation check) |
| ✚ model-calls per arm | trials in arm | A | $C_0$ (manipulation check) |
| ✚ opportunity yield per trial | trials in cell | A | $C_0$ (**Campaign 0 output**) |
| reference validity rate | extracted references | A | $C_0$ — **1.0 on all 107 trials; zero discrimination. Regression guard, not a measurement** |

---

## 4. Sample sizes — method preserved, inputs no longer invented

### 4.1 The method

`evallab.cohort`: $n = z^2\sigma^2/\Delta^2$ with
$\sigma^2 = p_0(1-p_0)+p_1(1-p_1)-2\rho\sqrt{p_0(1-p_0)p_1(1-p_1)}$,
$z = \Phi^{-1}(1-\alpha/2)+\Phi^{-1}(\text{power})$;
`minimum_detectable_effect` by bisection.

**Structural floors (mandatory):** $L \ge 4$ arms, $M \ge 20$ task clusters,
$R \ge 3$ seeds, else `REFUSAL_UNDERPOWERED_STRUCTURAL_FLOOR`. Below 20 clusters
cluster-robust sandwich SEs are severely downward-biased (Cameron et al. 2008).

### 4.2 Three corrections to how I applied it

**(a) The independence transform is withdrawn (B3).**
`pass_at_k_probability(p,k) = 1-(1-p)^k` assumes independent attempts, and
independent-attempt pass@k is rejected in-corpus (*Don't Pass@k*,
arXiv:2510.04265, per Tutor). My rev-1 "$k>2$ costs power" table was derived
**entirely** from that transform, so it is demoted from finding to *planning
heuristic under an assumption we reject*. It is retained only as a caution
against buying power with repeats, and **no budget is sized from it**.
Reliability is reported as `pass_any_first_k` and `pass_all_first_k` separately —
never aliased.

Note the two $\rho$'s are different quantities and were not interchangeable in
rev 1: `pair_correlation` models correlation **between compared cohorts**;
attempt correlation within a task is the **ICC**. Both now appear explicitly.
**Cross-arm $\rho_{\text{arm}}$ is realizable only under identity preservation —
the same task identity must appear in every arm.** It is therefore **measurable
in Vertical A only; Verticals B and C are unpaired**, because B draws new
curve-only identities per depth level and C pairs each fault with its clean twin
rather than across fault classes. Where an identity is absent from any arm it
must be dropped from all arms or the estimate reverts to unpaired. **No pairing
gain may be assumed for B or C budgets.**

**(b) Repeats do not multiply sample size (B4).** With $k$ repeats and
intra-task $\text{ICC}=\rho_I$, the design effect is
$\text{DEFF} = 1+(k-1)\rho_I$ and
$$n_{\text{eff}} = \frac{nk}{1+(k-1)\rho_I}.$$
At $k=3$, $\rho_I=0.30$: $\text{DEFF}=1.6$, so three repeats purchase
$\approx 1.9$ independent observations, not 3. **$n$ is stated in task clusters;
trial counts are never presented as sample size.**

**(c) The analysis $n$ is opportunity-bearing trials, not trials (B5).** Every
rate is NULL at zero opportunity, so if opportunity yield is $y$, effective
$n \approx M \cdot y$. Yield is currently **unmeasured** for every construct.
Supplying a correct method with invented $p_0$ and assumed $y$ relocates the
guess rather than removing it.

### 4.3 Consequence: no budget is stated until Campaign 0 reports

Illustrative sensitivity only — **not a plan**, and every row assumes
$p_0 = 0.5$, $\rho = 0.5$ (**Vertical A only; B/C unpaired**), $y = 1.0$, all
three of which Campaign 0 exists to replace:

| $M$ clusters | MDE | note |
|---|---|---|
| 20 (floor) | 0.289 | screening only |
| 50 | 0.191 | |
| 84 | 0.149 | |

If $y = 0.3$, the $M=20$ cell yields $n_{\text{eff}} \approx 6$ and **no rate is
reportable at all**. That is the whole reason Campaign 0 precedes everything.

---

## 5. Campaigns

Every campaign requires the full control set before it is runnable: **oracle**
($r=1.0$), **NOP** ($r=0.0$), **adversarial mutant** ($r=0.0$), **trivial policy**
(always-act / always-block / always-retry ≈ chance), and for injected-fault arms
an **un-intervened twin** establishing $p_0$ on the same cohort.

Two gates apply to every cell: a **per-image preflight answer-key exclusion
gate** that scans and purges **all oracle code, ground-truth tables, and
intermediate dependency traces** from the agent build context — covering the
`oracle/` and `solution/` directories **and the `verifier/` and `tests/`
`golden.json` answer keys (Vector B)** — with the **verifier running out-of-band
on host, completely isolated from agent inspection** (B6, D11); and
**length-stratified or hazard-rate reporting** for any rate whose denominator
grows with trajectory length (C6 — verified 21.67 vs 12.03 mean actions for
failures vs successes).

### Campaign 0 — denominator yield and difficulty calibration (**runs first**)

- **Sole outputs:** measured **opportunity yield per trial per cell**, realized
  $p_0$, realized intra-task ICC, realized cross-arm $\rho$, and the
  **non-saturated difficulty band** per ladder.
- **Explicitly produces no capability, causal, or comparative result.** It is
  instrumentation, not evidence about the model.
- **Why it must be first:** it replaces the four invented inputs ($p_0$, $\rho$,
  ICC, $y$) on which every budget depends, and it establishes the non-saturated
  band that Campaign A would otherwise presuppose (B7).
- **Scope:** smallest cell set that estimates yield and $p_0$ with a stated
  interval — deliberately below the structural floors, and therefore **barred
  from reporting any rate**.
- **Decision changed:** whether Campaigns A–C are affordable at all, and at what
  $M$. If yield is low, the correct response is to redesign the opportunity
  definition, not to buy more trials.

### Campaign A — MCP-FuncDAG v2 depth ladder

- **Blocked on:** authoring MCP-FuncDAG v2 (fact 2). The current family cannot
  populate the process features this campaign exists to measure.
- **Arms:** depth, $L \ge 4$, at the non-saturated band Campaign 0 reports.
  **Held constant:** width, connected and disconnected distractor counts,
  operation distribution, trace schema.
- **Identities:** new curve-only tasks; certified easy/medium/hard remain
  byte-stable.
- **Preregistered:** `CEILING_SATURATION` is a valid reportable outcome
  ($d_{50} > d_{\max}$), not a failed run.
- **Decision changed:** which factor binds hardest (depth vs width vs distractor
  surface) → whether construct 2 is one measurable or three.

### Campaign B — context dilation and compaction

- **Blocked on:** authoring the Harbor-native dilation/compaction family. LOCA-Lean
  is unusable (fact 1); LOCA-bench is on HOLD at `39022d6`.
- **Arms:** padding volume, $L \ge 4$, plus a **volume-matched
  semantic-distractor arm**. Without both, horizon and interference are fused.
- **Manipulation checks, mandatory:** per-arm `prompt_cache_hit_rate` (C1 —
  padding alters the cached prefix, so "neutral" is semantic not operational) and
  per-arm **model-call count** (C2 — compaction spends calls the 0-compaction arm
  never spends, so constant *step* budget ≠ constant *call* budget). **Declare
  which budget the estimand holds and report both.**
- **C3 requirement:** pin the retrieval/injection path byte-identical across
  arms, or relabel the construct **harness memory policy** — no model has
  cross-context persistence, so anything "remembered" is re-injected tokens.
- **Decision changed:** whether long-context degradation on this cohort is a
  horizon effect or an interference effect. Today they are indistinguishable.

### Campaign C — fault persistence ladder and class contrast

- **Blocked on:** building the **MCP fault interceptor** (fact 3), materializing
  the certified family, and enabling the state journal
  (`--plugin evallab.harbor_state_journal:StateJournalPlugin`, `PYTHONPATH` with
  `src/`, Docker `--pid=host`, `--cap-add=SYS_PTRACE`).
- **Two separate analyses:** persistence $\{1,2,4,8\}$ → ratio scale → curve
  permitted; class {permission, not-found, timeout, malformed-output,
  silent-wrong} → ordinal → **`REFUSAL_ORDINAL_METRIC_INVALID`**, non-parametric
  adjacent risk differences with FWER control only.
- **Recovery credit requires the paired NOP-retry control to fail** at the same
  $k$ (D12).
- **Decision changed:** whether detection or repair is the binding constraint. If
  silent-wrong recovery sits far below the others, the lab should build detection
  benchmarks rather than recovery benchmarks.

### Campaign D — open-model telemetry validation (parallel, no extra runs)

- The DeepSeek V4 Flash lane is wired (`SecretSafeDeepSeekMiniSweAgent`, LiteLLM
  → `api.deepseek.com`, secret-mounted, network-pinned) and the IR captures
  reasoning content, logprobs, and sampling parameters into CAS.
- **Purpose:** confirm those fields populate end-to-end on Campaign 0's trials
  before anything depends on them.

### Inference contract for every ladder

1. **Ordinal ban:** log-logistic $d_{50}$ restricted to ratio-scale factors
   (`tokens`, `depth`, `count`, `time`).
2. **Monotonicity:** order-restricted/isotonic inference or global Kendall
   $\tau_b$ with $\text{LB}_{1-\alpha}(\tau_b) > 0$. Unadjusted adjacent-pair CIs
   inflate FWER to $1-(1-0.05)^4 \approx 18.5\%$ at $L=5$.
3. **Engine:** GEE with exchangeable working correlation + cluster-robust
   sandwich, or logistic with task-cluster bootstrap. GLMM for ICC/variance
   components only — never nested inside the bootstrap (Hauck–Donner separation
   at 0%/100% arms).
4. **Symmetric discrimination gate:** a feature reading identically across all
   arms of its own ladder is **refuted for that construct** and reported as a
   negative result, not dropped.
5. **Censoring:** $d_{50}$ outside the tested range is a bound, never an
   extrapolation.

---

## 6. Deliberately deferred, with unblock conditions

Tutor §5 marks this section correct; extended with the new blockers.

| Deferred | Order | Why | Unblocks when |
|---|---|---|---|
| recovery × context interaction | **3** | stratifying halves $n$; at $M=20$ MDE 0.289 → ~0.4 in two strata | $M \ge 60$–80 **measured in opportunity-bearing trials** |
| tool → recovery elicitation | **3** | as above, plus both constructs instrumented in one campaign | after A and C land |
| all trajectory-process features for construct 2 | 0–2 | current family has no discrete tool surface (fact 2) | MCP-FuncDAG v2 exists |
| post-compaction re-read rate | 2 | needs state evidence to exclude changed files | a context campaign with state journal |
| divergence $k^*$, dose slope $\beta$, $\text{pass}^k$ contrasts | **4** | require matched arms | after any ladder materializes |
| `cot_action_divergence` (general form) | — | judge-based; narrow surrogate available (§7) | never in general |
| `harness_truncation_events` | — | loss manifest is **parse-side only** | runner-side instrumentation |
| reference validity as a measurement | 1 | 1.0 on all 107 trials | keep as regression guard |
| any cross-construct or cross-benchmark comparison | — | unbounded common harness term (C4) | a second harness configuration |

---

## 7. Answers to Tutor's five computability questions

| Question | Answer |
|---|---|
| `cot_action_divergence` | **Narrow surrogate computable; general form not.** `step.reasoning_content` is `preserved / cas_blob / lossless`. Surrogate: extract path/identifier tokens from reasoning text, check whether they appear as arguments in the *next* tool call — a **named-entity carry-through rate**, Grade **B**, same construction as reference validity. General intent↔action matching is judge-based: teach as unmeasurable. |
| `harness_truncation_events` | **No.** The 71-field manifest records **parse-side field disposition only**; no truncation entry exists. Harness-side truncation before injection is uninstrumented. Route to Eval Runner as an instrumentation request. |
| `schema_invalid_vs_semantically_wrong` | **Partially.** `HARNESS_SCHEMA_REJECTION` separates malformed calls from `COMMAND_NONZERO_EXIT`. But *schema-valid-wrong-target* is **not** a class: if it errors it lands in `COMMAND_NONZERO_EXIT`; if it silently succeeds on the wrong target it emits **no error signal at all**. That gap is exactly the silent-wrong cell Campaign C creates. Detection is regex over text → Grade **B**. |
| `subagent_depth` | **Computable, unimplemented.** `root.subagent_trajectories` and `step.observation_results[].subagent_trajectory_ref` are both `preserved / in_memory_ir / lossless`. Depth is a recursive walk over preserved refs — Order 0, trivial. |
| `prefix_cache_miss_attribution` | **Bounded today, attributable with token IDs.** Miss volume $=$ `prompt_tokens` $-$ `cached_tokens` (plus `cache_write_input_tokens` in `metrics.extra`). Attribution needs the prefix, and `prompt_token_ids` **is** preserved in the IR → longest-common-prefix across consecutive steps makes this Grade **A**, currently unimplemented. **This doubles as the C1 manipulation check.** |

---

## 8. Confound register — required manipulation checks

| # | Confound | Required check |
|---|---|---|
| C1 | Neutral padding is not cache-neutral: it alters the prompt prefix, hence cache hit rate, cost, latency, truncation exposure | report `prompt_cache_hit_rate` per arm; declare padding position relative to the cached prefix |
| C2 | Compaction consumes model calls the 0-compaction arm never spends | declare which budget the estimand holds; report **both** step and model-call budgets |
| C3 | "Actionable memory" silently becomes a harness measurement — nothing is remembered, only re-injected | pin retrieval/injection path byte-identical across arms, or relabel as harness memory policy |
| C4 | One scaffold carries all three verticals ⇒ unbounded common harness term | replicate one cohort under a second harness configuration, **or** state that no cross-construct comparison is licensed |
| C5 | Single model forfeits comparability | no capability wording; scope every statement to (model × harness × cohort) |
| C6 | Fast-crash denominator bias — failures run longer than successes (21.67 vs 12.03 mean actions) | length-stratified or hazard-rate reporting for every ladder; never a single pooled rate |

---

## 9. Defect disposition (Tutor round 1)

| Defect | Applies to my draft? | Disposition |
|---|---|---|
| **B1** unverified asset facts | **Partially** — I cited no arXiv ids, but my pins/licenses were agent-verified, not locally cited | §0.5 verification tiers; unsourced ids deleted; FuncBenchGen now LOCAL-VERIFIED at arXiv:2509.26553 |
| **B2** construct-1 primary under HOLD | **Yes** | Portfolio inverted (§1): Harbor-native family primary, LOCA fallback contingent on `39022d6` |
| **B3** pass@k independence vs $\rho$ | **Yes** | Independence transform withdrawn; $k$-scaling demoted to heuristic; `pass_any_first_k` / `pass_all_first_k` reported separately; the two $\rho$'s distinguished (§4.2a) |
| **B4** repeats inflate nominal $n$ | **Yes** | $n_{\text{eff}} = nk/(1+(k-1)\rho_I)$ stated; $n$ in clusters only (§4.2b) |
| **B5** guessed inputs, wrong denominator | **Yes** | **Campaign 0** inserted as first campaign; no budget stated until it reports (§4.3, §5) |
| **B6** container ships the solution | **Yes** | Mandatory per-image preflight answer-key exclusion gate covering `oracle/`, `solution/`, and the `verifier/` and `tests/` `golden.json` files; verifier runs out-of-band on host; verified layout discrepancy remains unsettled (§1, §5) |
| **B7** presupposes the ceiling | **Partially** — rev 1 required re-measuring the window and preregistered censoring | Difficulty calibration folded into Campaign 0; `CEILING_SATURATION` preregistered per ladder |
| **D8** arm arithmetic | **No** — rev 1 had no multi-factor budget table | Single authoritative cell inventory deferred to Campaign 0 output |
| **D9** cost inconsistency | **No** — rev 1 stated no costs | Cost to be derived as ceiling = cap × trials × cited price |
| **D10** causal/capability wording on $C_0$ | **Yes** | §0.3 bars "capability"; every statement scoped to (model × harness × cohort) |
| **D11** silent-wrong needs out-of-band oracle | **Yes** | Reference computed verifier-side, excluded from agent image (§2) |
| **D12** transient clearing credits blind retry | **Yes** | Recovery credited only when the paired NOP-retry control fails at the same $k$ (§2, §5) |

---

## 10. What would make me wrong

1. If Campaign 0 shows opportunity yield near 1.0 and $p_0$ in a workable band,
   my insistence on measuring before budgeting was over-cautious and cost a cycle.
2. If MCP-FuncDAG v2 proves expensive to author, the pragmatic path is
   artifact-level value propagation on the current family plus FuncBenchGen for
   process features — accepting two cohorts instead of one.
3. If the injected fault family diverges sharply from Recovery-Bench's real
   traces, denominator-by-construction was bought at the price of external
   validity and construct 3 should invert to Recovery-Bench primary despite its
   11/20 replay failures.
4. If the harness term (C4) turns out to dominate any measured ladder effect,
   then the single-harness constraint invalidates all three verticals and the
   first necessary purchase is a second harness, not a second construct.
5. If intra-task ICC is far above 0.30, repeats are worth even less than §4.2b
   states and the seed budget should collapse to $R=3$ purely for
   $\text{pass}^k$, with all remaining spend moved into task clusters.

Item 4 is the one I would check first, because it is the only one that
invalidates the whole program rather than one vertical.
