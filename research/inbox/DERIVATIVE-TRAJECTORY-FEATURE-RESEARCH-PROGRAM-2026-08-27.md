---
type: research-program
topic: derivative-trajectory-features
author: analyst
date: 2026-08-27
status: proposal
epistemic: recommendation
collection: trajectory-analysis
reviewed: 2026-08-27
depends_on: research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md
grounded_by: coverage audit 2026-08-27 (traj_features 99 rows x 39 cols; 9 zero-row fact schemas)
constraints: no code in this artifact; no LLM-judged labels anywhere in the program
---

# Derivative Trajectory-Feature Research Program

Operational counterpart to the Librarian's literature map. The map supplies
source-verified definitions and confounds; this document supplies the
**measurement program**: what we compute, against which denominator, at which
evidence grade, for which research use, and which synthetic recipe each feature
seeds. Plus the three studies to run first.

## 0. Governing rules

1. **Grade vocabulary is the map's**, not a parallel scheme:
   - $C_0$ — deterministic from raw ATIF + state journal + exit codes + digests.
   - $C_1$ — deterministic *given a task contract* (target-file whitelist, declared
     invariant, declared tool DAG, declared act/abstain polarity).
   - $C_2/C_3$ — requires interventional design (single-delta arms, fault
     injection, certified replay). **Only $C_2/C_3$ licenses causal language.**
2. **No LLM-judged labels.** Any feature whose value requires a model to
   interpret prose is excluded from this program, not deferred. See §10 for the
   three map items this bars and their permitted substitutes.
3. **Every rate carries its denominator as a sibling column.** $|\Omega| = 0
   \Rightarrow \texttt{null}$, never $0.0$ or $1.0$.
4. **$C_0$ features are screening unless paired with a contract or an arm.**
   Naming convention `*_screening` is retained from `traj_baseline.py`.
5. **No cross-benchmark pooling.** `query_scorecard` already refuses it; the
   program inherits that refusal.

## 1. Layer definitions

| Layer | Contents | Grade ceiling | Purpose |
|---|---|---|---|
| **L1 — baseline measured** | Per-trial scalars/series extracted once, task-agnostic, no contract required | $C_0$ | Universal description of any run. The table stakes. |
| **L2 — derived factors** | Opportunity-conditioned rates, ratios, curves, alignments over L1 | $C_0$/$C_1$ | Analysis-ready quantities; inputs to statistics |
| **L3 — task-generating recipes** | Parameterized environment transforms producing dose ladders or single-delta pairs | $C_2$/$C_3$ | Licenses causal claims; seeds synthetic families |

L1 is computed for **every** trial regardless of research question. L2 is computed
where its denominator exists. L3 is authored deliberately, one construct at a time.

---

## 2. Context & memory

**Objective:** decide whether observed failure is caused by token horizon,
compaction loss, or interference — and locate the threshold where retention breaks.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | `prompt_tokens` series per step; peak occupancy | — | $C_0$ | occupancy curves; window-fraction |
| L1 | `is_copied_context` count + step positions | — | $C_0$ | compaction boundary detection (**real ATIF field**, present in `steps`) |
| L1 | `cached_tokens` / `prompt_tokens` per step | prompt tokens > 0 | $C_0$ | cache efficiency; cost attribution |
| L1 | `reasoning_tokens` per step | steps with LLM call | $C_0$ | blocked on P1 |
| L1 | `observation_size_bytes` per step | — | $C_0$ | injected-volume accounting |
| L2 | **Post-compaction re-read rate** — paths read before first compaction, unchanged per state journal, read again after | paths read pre-compaction ∧ unchanged | $C_0$ | primary compaction-loss measure; no LLM required |
| L2 | **Information half-life** — steps between first read of a path and its last use | paths read ≥ 1 | $C_0$ | retention decay shape |
| L2 | **Redundant-read ratio** — repeat reads with no intervening change to that path | total reads | $C_0$ | thrash vs re-grounding |
| L2 | **Occupancy at first error** | trials with ≥ 1 error | $C_0$ | correlational only; never causal |
| L2 | **Constraint survival across compaction** | active constraints exposed pre-compaction | $C_1$ | requires declared constraint set |
| L2 | **Retention under dilation** $R(L)$ | tasks per dilation level | $C_2$ | threshold estimation |

**L3 recipes**
- *Neutral-padding dilation ladder* — single delta: padding volume $\in \{0, 16k, 32k, 64k, 128k\}$ of structured non-semantic filler. Mandatory paired **semantic-distractor arm** at matched token volume to separate horizon from interference (LOCA anti-confound).
- *Needle-position ladder* — single delta: relative position $p \in \{0.05, 0.25, 0.5, 0.75, 0.95\}$; volume held constant.
- *Forced-compaction ladder* — single delta: window cap inducing $\{0,1,2,4\}$ compactions; **step budget held constant**.
- *Two-session state inversion* — $K \to V_1$, then $K \to V_2$, then require $K$ in a tool argument. Tests actionable memory, not string recall.

---

## 3. Tool selection, composition & dependency

**Objective:** separate "chose the wrong tool" from "wired arguments wrong" from
"ignored a prerequisite" from "drowned in tool surface."

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | tool-mix histogram; unique tools; call count | — | $C_0$ | descriptive; entropy input |
| L1 | `(tool, arguments_sha256)` distinct-pair count | tool calls | $C_0$ | Loop Index input |
| L1 | `tool_call_id` → `source_call_id` edges | tool calls with observation | $C_0$ | observed dependency graph |
| L1 | harness schema-rejection events | tool invocations | $C_0$ | blocked on P1 (currently conflated with exit codes) |
| L1 | calls to unregistered tool names | tool invocations | $C_0$ | blocked on P4 (registry cross-check) |
| L2 | **Payload Loop Index** $LI = (N-D)/N$ | $N > 0$ | $C_0$ screening | loop screening only — high volume ≠ thrash |
| L2 | **Schema conformance rate** | tool invocations | $C_0$ | argument-construction competence |
| L2 | **Tool-selection entropy** $H(T)$ | tool calls | $C_0$ | exploration breadth; not quality |
| L2 | **Redundant-call ratio** — calls yielding no state change and an already-seen observation digest | tool calls | $C_0$ | wasted motion, deterministic |
| L2 | **DAG conformance** — observed ∩ required edges / required edges | declared DAG edges | $C_1$ | composition competence |
| L2 | **Prerequisite-violation rate** | applicable prerequisite pairs | $C_1$ | ordering competence |
| L2 | **Value-propagation accuracy** — output of $A$ appears as input of $B$ | required propagations | $C_1$ | wiring vs selection separation |

**L3 recipes**
- *Distractor-surface factorial* — the corrected form of "50 near-duplicate APIs." Three factors varied **independently**: (a) tool count with distinct names; (b) name-similarity at fixed count; (c) total schema token volume at fixed count. Varying them jointly is uninterpretable and is the single most common design error here.
- *Hidden function-DAG suite* — depth $\in \{2,4,8\}$ at fixed width, then width $\in \{2,4,8\}$ at fixed depth.
- *Specification-drift hazard* — single delta: runtime argument key renamed while docstring unchanged; measures adaptation from traceback.
- *Prerequisite trap* — tool $B$ succeeds but corrupts state unless $A$ ran; Grade-A state verification of the corruption.

---

## 4. Error & recovery

**Objective:** determine whether the binding constraint is *detection*,
*diagnosis*, or *repair* — and whether apparent recovery is autonomous.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | exit-code distribution | tool calls with exit code | $C_0$ | blocked on P2 |
| L1 | error count **excluding expected-negative exits** | tool calls | $C_0$ | blocked on P2 — currently corrupted |
| L1 | error class (schema / not-found / permission / timeout / network / syntax / assertion / OOM) by deterministic stderr+exit mapping | error events | $C_0$ | blocked on P2; **string→class table, not a model** |
| L1 | first-error index and timestamp | trials with ≥ 1 error | $C_0$ | localization |
| L1 | max non-zero cascade length | tool calls | $C_0$ | containment screening |
| L1 | intervention provenance per action (autonomous / assisted) | actions after an error | $C_0$ | mandatory recovery gate |
| L2 | **Blind-retry rate** — identical tool + identical args after non-zero exit | post-error retries | $C_0$ | thrash vs backoff |
| L2 | **Strategy-mutation rate** — changed argument skeleton after error | post-error retries | $C_0$ | adaptation signal |
| L2 | **Diagnosis grounding** — error-relevant artifact read before next mutation | errors followed by ≥ 1 mutation | $C_0$ | blind repair detection |
| L2 | **Recovery latency** — steps/tokens to next progressing action | recovered faults | $C_0$ | cost of recovery |
| L2 | **Post-error token overhead** | total tokens | $C_0$ | efficiency of repair |
| L2 | **Certified recovery rate** — invariant restored ∧ goal completed | **injected fault opportunities** | $C_3$ | the only defensible "recovery" claim |
| L2 | errors unrecovered at terminal | trials with ≥ 1 error | $C_0$ | termination quality |

**L3 recipes**
- *Fault-class factorial* — identical base task, single injected fault, class $\in$ {permission, not-found, timeout, malformed output, **silent wrong result**}. Silent-wrong is the discriminating cell: exit code 0, so only detection can catch it.
- *Transient-persistence ladder* — `transient_fail_count` $\in \{1,2,4,8\}$; locates the give-up threshold.
- *Assistance ablation* — identical fault with and without a user hint turn. Isolates autonomous recovery by design rather than by post-hoc filtering.
- *Invariant-corruption suite* — `chmod 000`, broken symlink; requires certified state restoration ($C_3$).

---

## 5. Verification, grounding & reference validity

**Objective:** measure whether the agent checks its own work, and whether the
artifacts it names actually exist — without ever grading prose.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | agent's own test/inspect invocations, count + positions | — | $C_0$ | verification behavior |
| L1 | any verification action after last mutation | trials with ≥ 1 mutation | $C_0$ | terminal verification flag |
| L1 | references to nonexistent paths (regex-extracted token, existence-checked) | extracted path-like references | $C_0$ | blocked on P4 |
| L1 | cited `file:line` beyond file length | extracted citations | $C_0$ | blocked on P4 |
| L1 | verifier exit code / reward | — | $C_0$ | outcome |
| L2 | **Verification-after-mutation ratio** | mutating actions | $C_0$ | primary verification measure |
| L2 | **Grounding ratio** — files inspected ∩ modified / modified | modified files | $C_0$ | blind-editing detection |
| L2 | **Reference validity rate** | extracted references | $C_0$ | deterministic stand-in for "hallucination" |
| L2 | **Vain-verification rate** — verification ran, returned non-zero, no subsequent mutation, then terminated | verifications returning non-zero | $C_0$ | ignored-evidence detection, **no prose reading** |
| L2 | **Contract-invariant check rate** | declared invariants | $C_1$ | requires declared contract |

**Explicitly barred:** citation entailment (ALCE/MiniCheck-style NLI) and any
"unsupported terminal claim" feature requiring claim classification. See §10.

**L3 recipes**
- *NOP-test trap* — a provided test script that always exits 0; genuine verification requires writing real checks.
- *Silent-corruption task* — surface verifier passes while a hidden invariant breaks unless properly verified.
- *Phantom-reference bait* — near-miss paths (`config.yml` vs `config.yaml`) present; measures reference precision deterministically.
- *Verification-cost ladder* — single delta: test-suite runtime $\in \{1s, 10s, 60s, 300s\}$; does verification behavior decay with cost?

---

## 6. Restraint, abstention & termination

**Objective:** distinguish correct restraint from degenerate refusal, and
intentional termination from harness death.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | terminal reason taxonomy (self-terminated / budget exhausted / harness kill / exception) | — | $C_0$ | blocked on P3; **must not conflate exit 124 with surrender** |
| L1 | terminal-function call (`submit`/`finish`) present | — | $C_0$ | intentional termination |
| L1 | post-terminal tool invocations | trials with terminal marker | $C_0$ | leakage |
| L1 | step/token budget consumption ratio | trials with declared limit | $C_0$ | blocked on P3 |
| L1 | machine-checkable abstention token present | tasks defining a token | $C_0$ | abstention record |
| L1 | critical/mutating action executed (`rm`, `git push`, `transfer_funds`, …) | — | $C_0$ | safety violation detection |
| L1 | terminal message **presence and length only** | — | $C_0$ | silent-termination measure |
| L2 | **Paired CAR** — act-correct on $T^+$ ∧ abstain-correct on $T^-$ | matched pairs | $C_1$ | defeats constant-refusal degeneracy |
| L2 | **FAR** on $T^-$ (mutation executed) | $|T^-|$ | $C_1$ | safety |
| L2 | **FRR** on $T^+$ (abstained) | $|T^+|$ | $C_1$ | helpfulness |
| L2 | **Premature-termination rate** — terminated ∧ budget remaining ∧ no verification ∧ unresolved error | trials | $C_0$ | conjunction of $C_0$ facts, no interpretation |
| L2 | **Silent-termination rate** — empty terminal message | trials | $C_0$ | the defensible remnant of the A2 investigation |

**L3 recipes**
- *Single-delta precondition severing* — byte-identical $T^+/T^-$ differing in exactly one scalar; enforced by `SingleDeltaAdmissionGate`.
- *Ambiguous-target task* — two plausible referents; correct behavior is clarification, and only a structural clarification turn counts.
- *Turn-budget ladder* — allowed turns $\in \{3,5,10\}$; measures horizon planning.
- *Zombie-process check* — post-terminal container process table inspection.

---

## 7. State & edit dynamics

**Objective:** separate focused construction from churn, thrash, and collateral damage.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | files created / modified / deleted; byte deltas | — | $C_0$ | state journal |
| L1 | lines added / deleted per path (CAS before/after diff) | modified paths | $C_0$ | churn input |
| L1 | mutations per action; unattributed mutations | state events | $C_0$ | background-noise control |
| L1 | action→effect links | actions | $C_0$ screening | **temporal, not causal** — keep labeled |
| L2 | **Code churn ratio** $(\Delta_{add}+\Delta_{del})/|\Delta_{net}|$ | $|\Delta_{net}| > 0$ | $C_0$ | wasted-edit measure |
| L2 | **File reversion count** — digest returns to initial | modified paths | $C_0$ | rollback loops |
| L2 | **Read-before-write compliance** | mutated paths | $C_0$ | grounding |
| L2 | **Idempotency violations** — identical action, different delta | repeated identical actions | $C_0$ | determinism of agent effects |
| L2 | **Unintended-touch rate** | modified paths | $C_1$ | requires target whitelist |

**Mandatory filter:** exclude `.pytest_cache`, `*.pyc`, `.git/index.lock`, and
build artifacts, or mutation counts are dominated by tooling noise.

**L3 recipes**
- *Collateral-damage suite* — verifier asserts zero modification outside the declared target module.
- *Multi-file coherence ladder* — files requiring consistent joint edit $\in \{1,2,4,8\}$.
- *Reversion bait* — a plausible-but-wrong edit that must be undone; measures rollback competence.

---

## 8. Efficiency & budget

**Objective:** price competence, and detect when spend stops buying capability.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | prompt / completion / cached / reasoning tokens; cost; duration | — | $C_0$ | reasoning tokens blocked on P1 |
| L1 | per-step latency | consecutive step pairs with timestamps | $C_0$ | blocked on P3 |
| L1 | steps to first tool / first mutation / first verification | — | $C_0$ | onset dynamics |
| L2 | **Cost per solved task**, reported separately for pass and fail | solved / unsolved trials | $C_0$ | **never pooled** — pooling is the fast-crash trap |
| L2 | **Productive-token fraction** — tokens in steps producing state change or new observation digest | total tokens | $C_0$ | waste measure |
| L2 | **Reasoning-token elasticity** — success vs realized reasoning tokens/step | trials at each reasoning level | $C_2$ | Study 1 |
| L2 | **Marginal step value** — $P(\text{success} \mid \text{reached step } k)$ | trials reaching step $k$ | $C_0$ | where progress stops accruing |
| L2 | **Budget-exhaustion rate**, separated from crash | trials | $C_0$ | denominator hygiene |

**L3 recipes**
- *Budget-cap ladder* — step cap $\in \{0.5\times, 1\times, 2\times, 4\times\}$ oracle solution length; yields the capability-vs-budget curve.
- *Latency-injection ladder* — tool latency $\in \{0, 1s, 5s, 20s\}$; does strategy adapt or thrash?

---

## 9. Delegation (evidence-limited)

**Honest status:** the only $C_0$ evidence we hold is ATIF `subagent_trajectories`
structure plus `observations.subagent_ref_count`. Everything causal here is $C_2$
and unbuilt. This domain is included for completeness and ranked last.

| Layer | Feature | Denominator $\Omega$ | Grade | Research use |
|---|---|---|---|---|
| L1 | subagent count; tree depth; child token/cost sums; child outcomes | — | $C_0$ | descriptive |
| L2 | **Delegation overhead ratio** — child tokens / total | trials with ≥ 1 subagent | $C_0$ | is delegation paying? |
| L2 | **Context-scoping ratio** — child prompt tokens / parent cumulative | child spawns | $C_0$ | over-provisioning |
| L2 | **Post-delegation rework** — parent re-derives child output (digest overlap) | completed child branches | $C_0$ screening | attribution is temporal |
| L2 | **Failure-isolation rate** — child failed ∧ parent repaired ∧ trial passed | child failure events | $C_1$ | resilience |

**L3 recipes:** fan-out ladder $\{1,2,4,8\}$ independent subtasks; shared-file
conflict tasks; split-feature integration with a joint verifier.

**Category-error warning:** our own builder-fleet trajectories are a far richer
delegation corpus than benchmark runs, but those are *our* agents under *our*
scaffold. They inform engineering throughput, never model-capability claims.

---

## 10. Items barred by the no-LLM-labels constraint

Three entries in the literature map's mart schema cannot be produced under this
constraint. Named explicitly so nobody silently reintroduces them:

| Barred item | Why | Permitted substitute |
|---|---|---|
| `unsupported_terminal_claim` (Claim = Success ∧ Verifier = Fail) | Requires classifying the terminal claim from prose. This is exactly the A2 track: six adversarial rounds, 29 findings, terminal HOLD. | 2×2 contingency of **terminal-message presence/length** × **verifier outcome**. Report the cell counts; assert no claim label. |
| `grounded_citation_coverage_pct` (ALCE/MiniCheck entailment) | NLI model = LLM judgment. | **Reference validity rate** (§5): regex-extract artifact tokens, check existence. Existence is $C_0$; entailment is not. |
| Clarification-quality / decomposition-quality scoring | Prose interpretation. | Structural clarification-turn presence only, and only where the harness marks user-directed turns. |

The boundary rule: **mechanical token extraction plus environment existence
checking is $C_0$. Semantic interpretation of what the agent meant is barred.**

## 11. Corrections to hand back to the mart schema

1. `unsupported_terminal_claim` — violates the constraint (§10). Replace.
2. `context_compaction_events` — defined as "system steps with context_management,"
   which we do not emit. Ground it on **`steps.is_copied_context`**, which exists
   and is already projected.
3. `schema_error_count` — defined via "non-zero exit codes on schema parse,"
   conflating harness schema rejection with ordinary command failure. Needs the
   distinct harness-rejection event from P1.
4. **Every rate column lacks a denominator column.** The map states the
   zero-opportunity null rule in its own §0 but the schema does not carry $\Omega$.
   Add `<feature>_denominator` for each rate, and enforce null-on-zero.

## 12. Instrumentation dependencies

| Blocked on | Features gated |
|---|---|
| **P1** parser fidelity | reasoning tokens, thinking:answer ratio, logprobs, sampling params, harness schema rejections, `finish_reason` (truncation control) |
| **P2** error semantics | exit-code distribution, expected-negative-excluded error rate, error classes, first-error index, recovery latency, unrecovered-at-terminal |
| **P3** materialization | per-step latency, budget utilization, peak occupancy, terminal-reason taxonomy |
| **P4** reference validity | nonexistent path/tool references, invalid `file:line` |
| None | most of §7 state dynamics, §3 loop/entropy, §6 critical-action detection |

---

## 13. First three studies, ranked

Ranking criteria: evidence grade achievable now, instrumentation dependency,
diagnosticity, and whether the result changes a decision.

### Study 1 — Reasoning-token elasticity (open-model only)

Ranked first: needs only **P1**, the treatment factor is directly settable, and
it exploits precisely the open-model advantage motivating the pivot.

- **H1:** success probability is monotone non-decreasing in realized reasoning
  tokens per step up to a threshold $\tau$, then plateaus or declines
  (saturating or inverted-U).
- **H1b:** higher reasoning volume substitutes for environment probing — tool
  calls per solved task decrease as reasoning tokens rise.
- **H0:** no monotone relationship once step count is conditioned on.
- **Cohorts:** identical Harbor-hosted task set; treatment = reasoning budget at
  $\geq 3$ levels; model families DeepSeek (exposes `reasoning_content` verbatim)
  and Gemini (`reasoning_effort`); 13-field comparability enforced.
- **Confounds and controls:**
  1. Reasoning tokens co-vary with total tokens, steps, and latency → condition on
     step count; report per-step.
  2. **Output truncation clips high-reasoning runs** → persist `finish_reason`;
     exclude or flag truncated trials, else survivorship inverts the curve.
  3. **Serving-flag artifact**: `reasoning_content` field vs `<think>` tags in
     content → normalize at parse or you measure the endpoint, not the model.
  4. `reasoning_effort` is a *request*, not a guarantee → regress on **realized**
     tokens, never the requested level.
  5. Cache hits change cost, not capability → report cost separately.
- **Minimum evidence:** P1 landed; realized reasoning tokens + `finish_reason`
  persisted; $\geq 3$ levels; per-cell $n$ from `required_tasks_for_effect` (computed,
  not guessed); expected-negative exits excluded; refuse-to-rank if CI covers zero.

### Study 2 — Compaction survival via post-compaction re-read

Ranked second: highest-interest construct, and the metric is novel, cheap, and
entirely free of prose interpretation.

- **H1:** crossing a compaction boundary raises the rate of re-reading
  previously-read, **provably unchanged** paths; the magnitude of that rise
  predicts subsequent task failure.
- **H0:** re-read rate is invariant across the boundary.
- **Cohorts:** identical tasks at forced-compaction levels $\{0,1,2,4\}$ (single
  delta = window cap); DeepSeek and Gemini; one agent scaffold only.
- **Denominator:** paths read before the first compaction event **and** unchanged
  since, per state journal.
- **Confounds and controls:**
  1. Re-reading a genuinely changed file is correct behavior → state journal
     exclusion is mandatory, which is why $C_0$ state evidence gates this study.
  2. Longer trajectories accumulate re-reads mechanically → normalize per step.
  3. Window caps also cut effective budget → hold step budget constant.
  4. Scaffold compaction policy differs → restrict to one scaffold.
  5. If padding is used to force compaction, run the **neutral and semantic
     arms both** — otherwise horizon and interference are fused.
- **Minimum evidence:** `is_copied_context` populated across the cohort;
  per-trial state-journal coverage (**currently 14 compact rows — fresh runs
  required**); deterministic path normalization; null on zero opportunity.

### Study 3 — Autonomous recovery by fault class

Ranked third: highest instrumentation dependency (**P2**), but its result most
directly seeds a synthetic family and answers a genuinely open question.

- **H1:** autonomous recovery rate varies by fault class and is **lowest for
  silent-wrong-result** — i.e. detection binds before repair.
- **H0:** recovery rate is invariant to fault class.
- **Cohorts:** fixed base task; single injected fault; classes {permission,
  not-found, timeout, malformed output, silent-wrong}; `transient_fail_count`
  $\in \{1,2,4\}$; DeepSeek and Gemini.
- **Denominator:** **injected** fault opportunities — known by construction. This
  is the whole reason injection beats natural observation.
- **Confounds and controls:**
  1. Assisted recovery must be excluded via `InterventionProvenance`, not filtered
     post hoc.
  2. Expected-negative exits must never enter the fault set.
  3. Transient auto-clearing is not adaptation → credit recovery only with a
     changed argument skeleton; otherwise label blind retry.
  4. Silent-wrong carries exit 0, so its cell is partly a *detection-channel*
     comparison — that is the hypothesis, and it must be stated, not hidden.
  5. Difficulty held constant by single-delta injection.
  6. Fast-crash denominator bias separated from budget exhaustion.
- **Minimum evidence:** P2 landed (error classes, expected-negative exclusion at
  the feature layer, autonomous flag reaching features); injection certified
  through the 8-point gate; certified state before/after for any invariant-restoration
  claim ($C_3$).

### Deliberately not first

- Anything requiring claim semantics (§10) — A2 is closed HOLD for cause.
- Plan- or decomposition-quality studies — no non-LLM operationalization exists.
- Delegation studies — $C_0$ evidence too thin; §9 caveat applies.
- Any pooled cross-benchmark score — refused by construction.

---

## 14. Handoffs

- **OMP Main / Architect:** this path, for scope approval and lane sequencing.
- **Agent Data:** §11 corrections before `v_trajectory_features` is materialized;
  §12 names which columns must wait on P1–P4 rather than shipping as nulls.
- **Analyst (me):** on unpause, author Study 1's design spec against whatever
  reasoning-token fields P1 actually lands, and compute per-cell $n$ from the
  power planner rather than assuming.
