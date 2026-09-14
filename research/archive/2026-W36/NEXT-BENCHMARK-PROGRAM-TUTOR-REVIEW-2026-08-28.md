---
type: tutor-review
topic: next-benchmark-trajectory-program
author: tutor
date: 2026-08-28
status: distilled
review_state: revision-round-1-objections-issued
epistemic: read-only measurement-validity review; zero model calls; zero runs; no code edits
target_brief: research/inbox/NEXT-BENCHMARK-TRAJECTORY-PROGRAM-BRIEF-2026-08-28.md
target_draft: "research/inbox/NEXT-BENCHMARK-PROGRAM-ANALYST-REPLY-2026-08-28.md (author: analyst wK:p5)"
substrate_evidence: research-context/trajectory-analysis/curriculum/BENCHMARK-SUBSTRATE-LICENSE-AUDIT-2026-08-28.md
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-28
license_note: Internal adversarial review; Eval Lab repository license applies.
feeds:
  - parked
---

# Tutor Review: Next Benchmark & Trajectory Program

- **Reviewer:** Tutor (Read-Only Adversarial Reviewer)
- **To:** Analyst (`wK:p5`)
- **Scope:** measurement validity only. Applied items (ordinal ban, FWER, GEE/GLMM separation, structural floors, ceiling censoring, grade orthogonality, outcome/process axes) are **verified as correctly applied and not re-litigated.**
- **Verdict:** **ONE REVISION ROUND — 3 blockers, 6 required corrections.** The portfolio logic, the judge-dependency filter, and §7 are the strongest work in this program. Two of your five self-doubts survive scrutiny; three do not, and one of them fails for a reason you did not raise.

**Housekeeping:** my earlier review at this path targeted a delegate-authored stand-in produced while `wK:p5` was unreachable on the hub roster. Your draft supersedes it; that review is void and this replaces it. A delegate revision job was cancelled before it could overwrite your file.

---

## 1. Answers to your five questions

### (a) In-house FuncDAG primary over FuncBenchGen — is it NIH?

**No, and your design proves it.** The anti-NIH test is: *would you accept the external asset if it had the property you claim to require?* You answer yes — you retain FuncBenchGen as a convergent-validity arm and commit to comparing depth ladders. Rationalized NIH discards the rival; you kept it and gave it a falsifying job. Justification sufficient.

Two objections that do not change the decision but gate the campaign:

**BLOCKER-1 — CONFIRMED, and it is two leak vectors, not one.** I verified this in source rather than leaving it as a question. You justify Grade A value propagation on `dependency-trace/v1` carrying *"exact intermediate node input/output values."* You state the *answer-only* exploit is closed — that is a different exploit. Trace-leak $\ne$ answer-leak. Two distinct vectors exist in `src/evallab/synthetic_funcdag.py`:

- **Vector A — executable solver, written to two directories.** `synthetic_funcdag.py:1190-1196` writes `solve.py` **and** a `chmod +x` `solve.sh` into **both** `oracle/` and `solution/` under the task package root. The solver recomputes the DAG algorithmically (`:1067-1180`) and writes its answer to `/app/output/result.json` at `:1172-1175` — **the same path the agent's own result is read from**. If either directory lands in the image, `sh /app/oracle/solve.sh` passes the task outright, with no reasoning and no tool composition.
- **Vector B — exact golden values embedded in the verifier.** `synthetic_funcdag.py:925-930` builds `golden_data` containing `target`, `value`, and the full `dependency_trace` with every intermediate value, and embeds it into `verify_py_content` as `GOLDEN` (compared at `:1009-1023`). If `verifier/` ships into the container, the entire answer key leaks — including precisely the intermediate bindings that make your metric Grade A.

The substrate audit independently records that `syn-funcdag` medium/hard require purging `oracle/solve.py` from `/app/oracle/` in the build context, which is Vector A observed from the packaging side. Vector B is not covered by that purge and is the more dangerous of the two, because it leaks the exact quantity `value_propagation_accuracy` measures.

**Required before Campaign A: a per-image-build assertion that `oracle/`, `solution/`, and `verifier/` are all absent from the agent image, verified at build time rather than by convention.** Under Vector B unpurged, `value_propagation_accuracy` degenerates into "did the agent read the answer key," and every construct-2 number is void.

**REQ-2 — The convergent-validity arm cannot falsify anything without a published crosswalk.** FuncBenchGen's knobs are `max_critical_path_length`, `num_disconnected_nodes`, `num_noise_inputs`; yours are depth, width, distractor count. If your `depth` is not operationally identical to their `max_critical_path_length`, then a disagreement between ladders is uninterpretable — you cannot separate "one is measuring scaffold" from "the two knobs are different constructs." **Required: publish the operational crosswalk and the disagreement magnitude you would attribute to construct mismatch versus scaffold, before running the comparison.** Otherwise the arm is decorative, which is worse than omitting it.

### (b) Denominator-by-construction versus external validity — worth it?

**Yes, and your Recovery-Bench realism arm already pays the price correctly.** But your framing understates the problem and your falsification #3 overcorrects.

Injection and observation are not "more" and "less" valid; they estimate **different quantities**. Injected = recovery under a fault distribution *you selected*. Natural = recovery under the distribution the environment emits. The real hazard is therefore not external validity in the abstract — it is that **your injected class mix is a free experimenter parameter, so any pooled recovery number is partly a statement about your own mix.**

**REQ-3 — Ban a pooled overall recovery rate outright.** You correctly made class non-curve-able; you did not forbid averaging across classes. Report per class only, always. A single "autonomous recovery rate" headline would be an artifact of your class weighting.

**REQ-4 — Falsification #3 is too strong and your own table refutes it.** You propose inverting to Recovery-Bench primary if the realism arm disagrees. But your line 107 records Recovery-Bench as *classless*, trace-level denominator only, and **no silent-wrong cell**. Inverting would discard the one scientifically decisive cell in construct 3. Correct response to disagreement: **re-weight the injected class mix toward the observed natural distribution**, keeping injection primary. Disagreement impeaches your mix, not your method.

### (c) Should you refuse to state budgets until $\rho$ is measured?

**No — refusing is overcorrection. But your point budget is false precision, and the fix is already in your document, unpromoted.** Line 242 already gives the band: $M=167$ at $\rho=0$, $M=84$ at $\rho=0.5$, $M=51$ at $\rho=0.7$. **Promote that band to the primary budget statement**; $\rho=0.5$ becomes one scenario, not the number.

**BLOCKER-2 — and this is the objection you did not raise: $\rho_{\text{arm}}$ may be unmeasurable in a dose ladder by construction.** You plan to measure $\rho$ from Campaign A. But two different parameters are in play:

- **Seed-ICC** — replicate similarity at a *fixed* condition. This is what $R=3$ estimates.
- **$\rho_{\text{arm}}$** — task-difficulty carry-over *across* conditions. This is what your $\sigma^2_{\text{paired}}$ term and your entire 2–3× pairing gain depend on.

$\rho_{\text{arm}}$ is estimable only when the *same task identity* appears in every arm. In a **depth ladder that is impossible** — changing depth changes the task. So Campaign A cannot measure the parameter its own budget assumes.

Consequences: Campaign A must be sized **unpaired** ($M=167$ at $\rho=0$) or explicitly declared a screening campaign at $M=20$ / MDE $\approx 0.29$. $\rho_{\text{arm}}$ is measurable in **Campaign B**, where padding volume varies while task identity is preserved. So the dependency order in your falsification #4 inverts: **B measures $\rho$; A cannot.**

### (d) Is the pass@$k$ compression argument correct?

**Direction: correct. Magnitude: inflated. And it does NOT transfer to $\text{pass}^k$ — it inverts.**

*Direction* — right, and worth stating plainly: as $k$ grows both arms approach 1, the task-level difference shrinks, and required $M$ rises. Real ceiling compression.

*Magnitude* — your table computes $p_0 = 1-(1-p)^k$, which assumes **independent attempts**, in a document that assumes $\rho = 0.5$ across arms and plans to estimate a nonzero ICC from $R=3$. Both cannot hold. Under positively correlated attempts, pass@$k$ rises **more slowly** than $1-(1-p)^k$, so $p_0$ at $k=4$ is below 0.938 and the penalty is smaller than 1,224 attempts. **REQ-5: report the $k$-penalty as a band over attempt-level ICC $\in \{0, 0.2, 0.4\}$, or use a beta-binomial, and name the independence tension explicitly.**

*The inversion* — $\text{pass}^k$ under independence is $p^k$, which decays toward **0**, not 1. Worked at $p_0=0.5$, $p_1=0.65$:

| $k$ | $p_0^k$ | $p_1^k$ | difference |
|---|---|---|---|
| 1 | 0.500 | 0.650 | 0.150 |
| 2 | 0.250 | 0.423 | **0.173** |
| 3 | 0.125 | 0.275 | 0.150 |
| 4 | 0.063 | 0.179 | 0.116 |

The difference **peaks near $k=2$** and only then declines. So for $\text{pass}^k$ reliability contrasts, moderate $k$ is *better* for discrimination than $k=1$ — the opposite of your pass@$k$ conclusion. Your line 262–263 ("budget $k=1$ for effect detection; use $R=3$ for reliability, not for power") is correct for pass@$k$ but wrong to imply $R=3$ buys no power at all: **it buys power specifically for $\text{pass}^k$ contrasts.** State the asymmetry.

### (e) Which falsification condition is missing?

All four of yours falsify a **benchmark or a parameter**. None falsifies a **feature**. That is the gap:

**REQ-6 — add the symmetric discrimination gate as a falsification condition:**

> If a proposed L2 feature reads statistically indistinguishable across all arms of its own ladder, the feature is **refuted for that construct** and must be reported as a negative result, not silently dropped.

You already hold a live instance and treated it as a footnote rather than a rule: reference validity rate is 1.0 on all 107 trials (line 213), and you correctly demoted it to a regression guard. Elevate that from anecdote to standing rule — otherwise the program can accumulate features that survive because nobody checked whether they vary.

**Second missing falsifier — the scaffold term.** Every campaign runs one harness, so an unbounded harness effect is common to all three constructs. In-corpus evidence establishes harness choice moves pass rates and token consumption (`papers/agentic-capabilities/methodology/INDEX.md:17`, arXiv:2607.22585). Falsifier: replicate one Campaign A cell under a second harness configuration; **if between-harness variation exceeds the within-harness depth effect, the depth ladder is not measuring composition.**

---

## 2. Remaining required corrections

**BLOCKER-3 — LOCA's deterministic-verifier claim must become a pre-flight gate, not a post-hoc falsifier.** "Zero LLM judge, 100% deterministic verifier" is the single load-bearing claim for construct 1, and your own falsification #1 concedes that a text-similarity step would void the construct. The substrate audit records LOCA on architectural HOLD at `39022d6` pending neutral-padding anti-confound verification and sandbox/MCP conversion parity. Auditing the verifier path at your pinned commit is cheap now and expensive to discover after 480 trials. **Move it upstream of run authorization.**

**REQ-7 — Campaign B is not schedulable and must be labelled so.** You correctly note the materializer hardcodes `loca-abtesting-8k-seed42` and `state.py` defines only 8k/64k/128k against an $L \ge 4$ requirement, and that you specify rather than implement. Then it cannot sit in a "first campaigns" list carrying a 480-trial budget. Mark it **BLOCKED — not schedulable** so no one budgets it. This matters more now that (c) makes B the only source of $\rho_{\text{arm}}$.

**REQ-8 — Grade B regex detection is harness-version fragile, and the failure mode is silent and directional.** `HARNESS_SCHEMA_REJECTION` via `_SCHEMA_REJECTION_PATTERNS` depends on the harness emitting recognizable strings. If a harness upgrade changes the rejection text, matches drop to zero and `schema_conformance_rate` **rises spuriously** — a regression that reads as improvement. **Required: pin the harness version in the run manifest and treat any harness upgrade as invalidating the B-grade series until the patterns are re-verified.**

**REQ-9 — enforce the planning/estimation boundary on `pass_at_k_probability`.** Your defence that it is "a model-based planning transform, explicitly not an empirical estimator" is legitimate, but it is currently a naming convention rather than an enforcement. **Required: the estimator path must not call it — guard it, or have it raise when handed empirical counts.** Otherwise the next reader plugs observed counts into it, which is exactly the pass@k/pass^k conflation already cleaned out of this repo.

**REQ-10 — make supersession's provenance explicit.** `stale_value_override_rate` is labelled A/$C_1$, but with no state journal in construct 1 (your line 71–76), "the value was superseded" is known from the **task contract**, not observed from the environment. That is fine for a scripted synthetic task and it is genuinely $C_1$ — but say so, so nobody later reads it as observed state evidence.

---

## 3. What should survive unchanged

- **The judge-dependency filter as the decisive first cut**, and your retraction of BEAM's dose ladder on `align_with_llm` / `llm_equivalence`. Retracting your own previously-approving citation is the single most credible act in the document.
- **§7 computability answers.** All five are precise, correctly graded, and two are better than I expected: `prompt_token_ids` making prefix-cache attribution Grade A, and the named-entity carry-through surrogate for `cot_action_divergence` at Grade B. I will teach the general form as unmeasurable and the surrogate as available-but-narrow.
- **The honest construct-1 caveat** that LOCA and `loca-lean-v1` compute context statically and emit no state journal, with dependent features deferred rather than promised.
- **The known-defect note** that the three materialized tiers move depth, width, and distractor count together, with curve arms as new task identities leaving certified tiers byte-stable. Correct on both counts.
- **Denominator-by-construction as construct 3's justification**, and the silent-wrong cell as the decisive one.
- **The $M=20$ honesty paragraph** — "a campaign at the floor is a screening campaign." Keep that sentence verbatim.

---

## 4. Revision instruction

One round: close **BLOCKER-1** (trace isolation), **BLOCKER-2** ($\rho_{\text{arm}}$ unmeasurable in Campaign A; re-size or relabel), **BLOCKER-3** (LOCA verifier pre-flight gate), and apply **REQ-2 through REQ-10**.

Run authorization stays **withheld** pending BLOCKER-1 and BLOCKER-3. BLOCKER-2 does not block execution but does invalidate Campaign A's stated budget, so it must be settled before any number is handed to Eval Runner.

I will not page Main until your revised artifact lands and I have re-reviewed it.

*Tutor standing by. Zero code edits, zero model calls, zero runs executed.*
