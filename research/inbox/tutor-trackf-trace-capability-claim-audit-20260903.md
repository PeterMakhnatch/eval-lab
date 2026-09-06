---
source_type: internal
type: claim-audit
topic: trace-capability-claims-track-f
author: tutor
date: 2026-09-03
status: distilled
epistemic: primary-source verified (PDF sha-pinned, repo fetched first-hand); every claim carries a source; forbidden language enumerated
brief: research/inbox/evals-overnight-core-capabilities-20260903.md, Track F
related: /private/tmp/eval-lab-briefs/tutor-trackf-claim-gates-20260903.md (gate G8)
---

# TRACE-Capability claims audit — Track F

## 0. Source pins (all first-hand unless marked)

| Source | Pin |
|---|---|
| Paper | `trace-capability-training.pdf`, **SHA-256 `89dde806e7f67e01dcdd9938cde873c55d4fd9256c6d7bdd08cc8c102dac634e`** — byte-verified against `manifest.json` entry (arXiv:2604.05336, 5,083,752 bytes) |
| Paper identity | *"TRACE: Capability-Targeted Agentic Training"* = *"Turning Recurrent Agent failures into Capability-targeted training Environments"*, Kang*, Suresh*, Saad-Falcon, Mirhoseini (Stanford). **arXiv:2604.05336v2 [cs.AI], 2 Jul 2026. Status banner: "Preprint. Under review."** |
| Repo | `github.com/ScalingIntelligence/TRACE` — **MIT License** (fetched via GitHub API today), Python; inventory pin `d2db23085409555b3f13ea426f42d62cf0bbc43d` |
| Paper↔repo discrepancy | τ² result: paper says **48.2%, +15.3**; repo README table says **32.9% → 48.3%, +15.4 pp**. SWE-bench rows differ in model: paper's +15 is **Qwen3-30B-A3B (41% Pass@1)**; repo headline row is **Qwen3.6-27B 68.0% → 73.2% (+5.2 pp, "best iter checkpoint")**. Quote numbers only with their (benchmark, model, source) triple. |

---

## 1. Claims ledger

### 1.1 SOURCE-VERIFIED — Track B/C may rely on these, with attribution

| # | Claim | Exact source (PDF reader line) |
|---|---|---|
| V1 | **Four-step loop:** (1) base-agent rollouts + LLM analysis agent contrasts success/fail, ranks by failure coverage; (2) per retained capability, an LLM generation agent builds a synthetic env **preserving the target interface** (tool schemas, protocol, output format), instances **procedurally generated from random seeds**; (3) LoRA adapter per capability via RL (GRPO); (4) MoE over adapters | §1 contributions; §3.3–3.4 (L49, L131–143) |
| V2 | **Deficit retention rule:** $\hat\Delta(c) = \hat{ER}^-(c) - \hat{ER}^+(c) \ge \delta$ **and** $\widehat{Cov}(c) \ge \rho$, with **δ = 0.20, ρ = 0.10**; ER rates are LACKING-frequency among **non-NA** labels, computed separately on failed ($D^-$) and successful ($D^+$) subsets | §3.3, L111–129, verbatim: *"We set ρ = 0.10 … and δ = 0.20"* |
| V3 | **Label ontology:** per (trajectory, capability) → **NA / PRESENT / LACKING**; NA excludes the trial from that capability's denominator | §3.3, L117 |
| V4 | **Two-phase identification:** *discovery* (induce a canonical capability dictionary) then *labeling* (fixed dictionary applied systematically); consistency via repeated labeling runs, **retaining only capabilities selected consistently** (discovery not repeated) | §3.3, L113, L129; repo Step 1: 10 parallel labeling subagents, **K-of-N** consistency |
| V5 | **Synthetic env contract:** seeded generator $x = G_c(z)$ deterministically constructs the instance; success **by construction** requires exercising $c$; capability exercise **verified automatically from tool arguments, state changes, or final outputs**; reward by **hash-based consistency checks** of final DB state vs ground truth | §3.3, L102–104, L131–133 |
| V6 | **Published results (as the paper's own, on its backbones):** τ²-Bench 48.2% (+15.3 vs base; +8.6 vs GEPA); SWE-bench Verified 41% Pass@1 (+15; +8.4 vs SWE-RL); sample efficiency <¼ rollouts (+10.4 / +8.6 over GRPO/GEPA on τ²); 2–4 LoRA adapters × 5.3% params; Qwen3.6-27B 73.2% Pass@1 SWE-V-V vs GPT-5.2-Codex 72.8%; capability-targeted training beats prompt-injection of the same capabilities by +8.6 / +10 | Abstract L13; §1 L54–61 |
| V7 | **GRPO anti-degenerate control:** rollouts share a seed per group; groups where **all rewards are identical are discarded** (no learning signal) | §3.4, L141 |
| V8 | **Repo artifacts exist:** `pipeline/aggregate_capabilities.py` (computes Cov/Δ, dual threshold, K-of-N), `pipeline/calibrate_environment.py` (rollout stats + pass@k check), `prompts/<benchmark>/{capability_selection,environment_generation}.md`, `selected_capabilities.json` schema (name, description, `mean_cov`, `mean_delta`, example failed cases, `status: "PENDING"`), phase1–phase6 reference run | Repo tree + README (fetched today) |
| V9 | **Feasibility pins:** ≥1 GPU for the selection-time vLLM server; **4+ GPUs recommended for GRPO** (TP=4, Qwen3.6-27B @128k ctx); pipeline stages are driven by an **LLM coding agent following markdown prompts** | README, Prerequisites + How TRACE works |
| V10 | ADP appears in TRACE's related work as *"unified public trajectories (e.g. ADP (Song et al., 2026b))"* — i.e., a public-trajectory dataset line, distinct from TRACE's own-target-environment approach | §2, L71 |

### 1.2 NOT SOURCE-VERIFIED — do not assert

- Any δ/ρ value **other than** 0.20/0.10, or δ/ρ attributed to Eval Lab results (they are TRACE's configuration, not a universal constant).
- "MIT" beyond the repo license file: patent/TLA branding claims are outside what I checked.
- Any TRACE behavior on Harbor, ATIF, τ³, BFCL, or Eval Lab verticals — TRACE was run on τ² (Airline+Retail per repo; paper says customer-service) and SWE-bench only.
- K-of-N consistency parameters (the repo README names the mechanism; exact N/K values were not extracted).
- Whether `aggregate_capabilities.py` is byte-deterministic (it computes fixed arithmetic over labels, but label inputs are LLM-produced; determinism holds only per fixed label set).

### 1.3 FORBIDDEN claims (anti-overclaims)

| Forbidden | Why | Required instead |
|---|---|---|
| "TRACE implementation" / "we reproduce TRACE" / "TRACE-powered" | No GRPO/LoRA/MoE stage exists in tonight's wave (brief invariant 8); V1 steps 3–4 absent | *"TRACE-style contrastive deficit mining (Kang et al., arXiv:2604.05336v2, preprint, under review) — selection stage only"* |
| Quoting +15.3 / +15 / 48.2% / 73.2% as anything but **the paper's self-reported results on its own backbones** | Preprint, under review; single-sourced; repo README already disagrees with the paper by 0.1 pp and substitutes a different model row | Attributed, triple-pinned: (number, benchmark, backbone, source=paper|repo) |
| "Capability X is missing/lacking" as a mechanical fact | The LACKING label is produced by an **LLM analysis agent** (V1, V3) | *"labelled LACKING by [annotator identity/model+prompt digest]; contrastive support Δ̂=…, Cov̂=… over n=…"* |
| Causal or general capability claims from a deficit artifact | The artifact's own contract (Track B acceptance) refuses unsupported causal/general claims | Phenotypic, bounded: *"recurring failure pattern observed in this corpus"* |
| δ/ρ as validated thresholds for Eval Lab | They are TRACE's choices on τ²/SWE corpora | *"TRACE's retention thresholds (δ=0.20, ρ=0.10) adopted as priors; to be calibrated on Eval Lab data before use as gates"* |
| Deterministic-pipeline claims for Step 1 | TRACE Step 1 is **prompt-driven LLM agents** (V9); only the Δ/Cov arithmetic is mechanical | *"LLM-labelled inputs + deterministic aggregation (Δ/Cov/thresholds), mirroring TRACE's aggregate_capabilities.py"* |

**The central claim boundary, stated once:** TRACE's capability identification is **LLM-judged** (an analysis agent produces the dictionary and labels); only the **arithmetic over labels** (ER rates, Δ, Cov, thresholds, K-of-N) is mechanical. Eval Lab's Track B may adopt the mechanical layer verbatim; adopting the labeling layer would violate brief invariant 6. Therefore `CapabilityDeficitArtifact` = deterministic arithmetic **over typed, digest-pinned label inputs**, with annotator identity in the artifact — never the labels themselves.

---

## 2. Adaptable metrics and contracts (exact definitions → Eval Lab binding)

| TRACE element | Exact definition (source) | Adaptable form for Track B/C |
|---|---|---|
| Contrastive gap | $\hat\Delta(c) = \hat{ER}^-(c) - \hat{ER}^+(c)$, ER = LACKING share among non-NA, per success subset (L121–125) | Deterministic field on `CapabilityDeficitArtifact`, computed **from label inputs**, never guessed. NA excluded from denominators — the same opportunity-denominator discipline as `feature_registry.py` `DenominatorPolicy`/`null_on_zero_denominator` |
| Coverage | $\widehat{Cov}(c) = \frac{1}{\|D^-\|}\sum \mathbf{1}[\ell_c = \text{LACKING} \wedge y = 0]$ (L127) | Second deterministic field; **requires a verified outcome label** (`verifier/reward.json`), i.e. G0-cleared trials only |
| Retention predicate | $C^* = \{c : \hat\Delta \ge \delta, \widehat{Cov} \ge \rho\}$, δ=0.20, ρ=0.10 (L127–129) | The **B→C promotion predicate**. Ship δ/ρ as *configuration with the paper provenance string*, not as constants — G8 requires they be calibrated before acting as gates |
| Consistency across runs | Labeling repeated, discovery fixed; retain only consistently selected capabilities (L129); repo: 10 subagents, K-of-N (V4) | Maps to: **pinned label-set version** in the artifact. With one frozen label set, "consistency across runs" degenerates to determinism — which is exactly Track B's idempotent-re-ingest acceptance. State this equivalence explicitly; do not claim multi-run consistency that was not run |
| Seeded generator | $x = G_c(z)$, one distinct scenario per seed, "preventing memorization" (L131) | Track C's "same seed is byte-identical" acceptance is the same contract — TRACE precedent, cite it |
| Auto-verified capability exercise | verified from tool arguments / state changes / final outputs (L102–104, L133) | Maps to Eval Lab's verifier classes: funcdag exact-state oracle, funcdag tool-argument checks. Strengthens G7: a TRACE-style candidate over Eval Lab engines inherits an **independent** verifier by construction |
| Hash-based state grading | reward by comparing final DB state to ground truth via hash-based checks (L133) | Already Eval Lab-native (`ToolCallFact.arguments_sha256`, `ObservationFact.content_sha256`; final-state comparators) — reuse, do not invent a second convention (brief invariant 1) |
| Zero-signal group discard | GRPO groups with identical rewards discarded (L141) | Adopt **now** as an experiment-spec clause for any future RL arm: identical-reward groups carry no signal — same spirit as `RefusalCode.ZERO_VARIANCE` (`src/evallab/analysis_capability.py`) |
| Difficulty calibration by mutation | repo Step 2: base-model pass rate too high → surface-disguising mutations (rename identifiers, inline helpers, decoy sites), re-test, ≤5 rounds; `calibrate_environment.py` pass@k check | Direct precedent for Track C's difficulty lever **and** for G1: same win-band motive. Borrow the *mutation-then-remeasure* loop shape; stay within Eval Lab's oracle/nop/fair_oracle/adversarial battery |
| `selected_capabilities.json` schema | name, description, `mean_cov`, `mean_delta`, example failed cases, `status: PENDING` (repo Step 1 output) | Reference shape for `CapabilityDeficitArtifact` fields `observed_support` / `counterevidence` / classification boundary — with `PENDING`→quarantine mapping to Track C's "candidates stay quarantined until existing admission gates approve" |
| Oracle self-test | repo Step 2: *"asserts the target test fails pre-fix and passes after applying the oracle"* | Independent confirmation that Eval Lab's **oracle/nop** battery is the standard validity pair — keep both sides in any candidate's validation plan |

---

## 3. What TRACE does **not** give us (anti-overclaim corollary)

1. **No trajectory ingestion into training.** TRACE consumes trajectories as *analysis input* (Step 1) and trains on **newly generated environments** (Steps 3–4) — it never fine-tunes on the collected trajectories themselves. "Train on traces" via TRACE therefore means *traces → deficits → new environments → RL*, which is exactly the brief's missing-middle shape, and **not** SFT on traces (that is FireAct/ReST$^{EM}$ lineage).
2. **No independent-verifier story.** Synthetic envs are self-validated (oracle self-test + rule-based checks + LLM checks) — the same self-grading property as SPADE, one level up. TRACE gives no certification layer; that remains Eval Lab's differentiator (G0/G7).
3. **No API-only path to its headline results.** Steps 3–4 require open weights + 4+ GPUs (V9). The diagnosis stage (Step 1) is API-only and is all Eval Lab can use tonight.
4. **No multi-environment evidence.** Two benchmarks (τ², SWE-bench), two backbones, preprint. Generalization beyond that is untested.

---

## 4. Track bindings (one line each)

- **Track B:** adopt V2/V3/V4 arithmetic over digest-pinned label inputs; `unclassified` default; annotator identity mandatory; δ/ρ shipped as configuration with provenance.
- **Track C:** candidates must carry parent-deficit digest + seeded-generator determinism + twin identity (already specced) + a declared real-held-out twin (gate G6) or remain non-trainable.
- **Track D:** SPADE-shaped consumer stays env-plan-only (gate G7); a TRACE-shaped consumer, if added later, is likewise **deficit→env-plan only**, never a trainer.
- **Track E:** ordering-prompt and other interventions remain paired-arm specs; TRACE results may not be used as expected-effect sizes (different envs, different backbones, preprint).

---

## 5. Artifact hashes

| Artifact | SHA-256 |
|---|---|
| This audit | computed at write time — see page-back message |
| `trace-capability-training.pdf` | `89dde806e7f67e01dcdd9938cde873c55d4fd9256c6d7bdd08cc8c102dac634e` (matches manifest) |
| `sources/synthetic/manifest.json` (eval-lab) | entry verified in-tree; full-file hash not recomputed this pass |
| Prior gates doc | `/private/tmp/eval-lab-briefs/tutor-trackf-claim-gates-20260903.md` |
| SPADE fit verdict | `/private/tmp/eval-lab-briefs/tutor-spade-fit-response.md` |
