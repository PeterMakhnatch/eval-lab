# Soundness best practices for agentic coding tasks (SWE-style: repo + instruction + hidden tests)

Research-only synthesis for the Traces lane, 2026-10-01. Web + paper research; no repo edits.
Exa was not needed (web_search + primary-source reads sufficed; skill `skill://exa-search` was read).
Every claim carries a source URL. Anything not directly observed is marked **[UNVERIFIED]**.

## 1. Master table

| Practice | Mechanical or judgement | Who/what does it (humans, model, which model) | Measured accuracy / agreement | Source |
|---|---|---|---|---|
| F2P/P2P validation by gold patch: tests fail before, pass after the reference fix; pre-existing passing tests stay green | Mechanical (execution) | Harness code (SWE-bench eval scripts) | No accuracy metric (definition of the benchmark); foundation for everything below | https://arxiv.org/abs/2310.06770 ; https://openai.com/index/introducing-swe-bench-verified/ |
| Human screen for underspecified issues + over-narrow/broad tests, 0–3 severity scale (Q1.1 issue spec; Q2.1 test scope; Q3.3 other issues; difficulty + confidence) | Judgement | 93 professional Python devs, 3 annotators/sample, max-severity ensemble; filter if ANY annotator scores ≥2 | Inter-annotator agreement (kappa etc.) is **not published** [UNVERIFIED whether it exists]; outcome: 38.3% flagged underspecified, 61.1% flagged unfair tests, 68.3% filtered overall; 1,699 → 500 | https://openai.com/index/introducing-swe-bench-verified/ ; https://cdn.openai.com/introducing-swe-bench-verified/swe-b-annotation-instructions.pdf |
| Automated task-quality classifier trained on the Verified annotations | Mechanical once trained; judgement distilled from humans | Fine-tuned Qwen2.5-72B-Instruct (3 binary heads: clarity / complexity / test correctness; 75/25 split, 413 validation) | Task complexity 81% acc (F1 0.82) vs 68% base; test-patch correctness 67% (F1 0.65); issue clarity 79% (F1 0.76) | https://arxiv.org/abs/2505.20411 (esp. §2.4) |
| Training-env validation: gold patch must pass more tests than base; hand-built envs | Mechanical + human labor | Authors (~200 human-annotation hrs + 10k CPU-hrs); Lite split additionally drops multi-file edits, vague statements, huge diffs, error-message tests | No classifier accuracy; downstream: +12–14pp on Verified/Lite from 491 trajectories | https://arxiv.org/html/2412.21139v2 (§3.1–3.2) |
| Synthetic-bug solvability: keep only candidates breaking ≥1 existing passing test; 2-min test cap | Mechanical (execution) | Harness code | Yield by strategy: combine 96.9%, LM-modify 56.0%, procedural 40.2%, LM-rewrite 35.0%, PR-mirror 33.8%; difficulty-rater 75.3% test acc; ~20 h human labor total | https://arxiv.org/html/2504.21798 (§2.1, Table 1) |
| Procedural envs from commits: F2P from existing tests, generated tests where missing; backtranslated statements using F2P tests in the prompt | Mechanical + model judgement | Heuristics + LLM filter for commits; backtranslation LM; decontamination by repo exclusion (4,578-task subset) | Synthetic ≈ real issues for training (27.8% vs 28.0% Pass@1); thoughts in trajectories 34.4% vs 30.4% without | https://arxiv.org/html/2504.07164v1 (§2–3) |
| Hybrid verification at test time (execution-based testing-agent + execution-free judge) | Both | Qwen-Coder-32B testing-agent (M=10 tests) + learned verifier | Each alone plateaus ~42–43%; hybrid reaches 51% on Verified | https://arxiv.org/html/2504.07164v1 (§4) |
| LLM-synthesized F2P tests (Gherkin description → code → traceback revision) + LLM patch-vs-reference trajectory filter | Both | Llama-3.1-70B (descriptions) + Qwen-2.5-Coder-32B (code); Llama-3.1-70B-Instruct 4-way similarity filter | Filter keeps ~65% of trajectories with parity-or-better performance (15.87 vs 15.80); final 7B 23.4% / 32B 36.6% on Verified | https://arxiv.org/html/2506.07636v2 (§3–5) |
| SFT trajectory distillation from gyms | Mechanical (keep only passing rollouts) | Teacher Qwen3-Coder-480B-A35B-Instruct via OpenHands; issues from SWE-Gym + R2E-Gym-Subset | Dataset card reports no filter-precision metric [UNVERIFIED beyond counts: 51,029 trajectories in table vs "59k" in prose] | https://huggingface.co/datasets/nvidia/Nemotron-SWE-v1 ; https://docs.nvidia.com/nemo/gym/infrastructure/engineering-notes/swe-rl-case-study |
| RL env hygiene: 6-container consistency (2× start-must-fail + 4× reference-must-pass); cleanup of build artifacts/caches/git history; verifier kept outside solver env | Mechanical (execution) | Pipeline code + construction agents | Ablation: quality-filtered 5k beats unfiltered 8k by +0.59pp (SWE-bench Pro), +4.59pp (DeepSWE), +4.49pp (held-out Val) | https://arxiv.org/html/2609.22068v1 (§3.1–3.3, §5.1) |
| Adversarial leakage probing + solution-review agreement + keep-only-mixed-outcome tasks | Judgement (model) + mechanical | Adversarial rollout agent + separate reviewing agent checking evidence vs reference/verifier; frontier-model rollouts | No published precision/recall for the filters; end-to-end: DeepSWE 10.0%→21.7%, Terminal-Bench v2.1 63.7%→72.2% after GRPO | https://arxiv.org/html/2609.22068v1 (§3.4–4) |
| Test-augmentation audit (UTGenerator + UTBoost) | Mechanical generation, execution verdict | LLM test generator over codebase + dependencies | 36 insufficient-test instances, 345 wrongly-passed patches; ranking churn: Lite 40.9% (18 changes), Verified 24.4% (11 changes) | https://arxiv.org/abs/2506.09289 |
| Differential patch testing (PatchDiff: generated vs gold behavior) + manual inspection | Mechanical diff + human judgement | PatchDiff tool; manual review of divergent cases | 7.8% of patches counted correct while failing dev tests; 29.6% behaviorally divergent (46.8% divergent impls, 27.3% over-adapting); 28.6% of divergent certainly wrong; +6.2pp inflation | https://arxiv.org/abs/2503.15223 |
| Solution-leakage / weak-test audit (SWE-bench+) | Human judgement | 3 authors, independent patch comparison, discussion-resolved disagreements (no kappa reported) | Of 251 passed patches: 32.67% solution leak, 31.08% weak tests; SWE-Agent+GPT-4 12.47% → 3.97% (abstract) / 5.49% (§2.2) **[paper-internal discrepancy, unresolved]**; Lite 18%→9.33%, Verified 22.4%→10.0%; fresh SWE-bench+ (548 post-cutoff issues): 0.55–3.83% | https://arxiv.org/html/2410.06992v2 (§1–4) |
| Agentic Benchmark Checklist (task validity / outcome validity / reporting) | Judgement (checklist applied by benchmark authors) | Author panels (UIUC/Stanford/Berkeley/MIT/Princeton/Transluce/others) | Misestimation up to 100% relative (paper abstract; project site says up to 40% **[discrepancy]**); ABC applied to CVE-Bench cut overestimation by 33%; Verified scored 50/100/30.8 on outcome/task/reporting | https://arxiv.org/abs/2507.02825 ; https://uiuc-kang-lab.github.io/agentic-benchmarks/ |
| Hand-written behavior verifiers + never-upstreamed reference solutions (DeepSWE) | Human judgement (verifier authorship) + independent LLM-judge audit | Task authors; independent LLM judge re-reviewing graded runs | Judge–verifier disagreement 1.4% vs 32.4% for inherited SWE-Bench-Pro tests; 113 tasks / 91 repos / 5 langs; reference patches touch 5.5× more code at ~½ prompt length | https://arxiv.org/abs/2607.07946 ; https://github.com/datacurve-ai/deep-swe |
| Oracle-must-pass + nop-must-fail gates on every task PR | Mechanical (execution) | Harbor CI (`/validate`): Docker build, oracle, nop | No accuracy metric; binary gate. Rubric-regression harness gates the LLM reviewer on 100% catch rate over planted-failure meta-tasks | https://github.com/harbor-framework/terminal-bench/blob/main/docs/TASK_REVIEW_AUTOMATION.md |
| Instruction ↔ test alignment review ("would the instruction alone let you write the same tests?") + anti-cheat trials | Human judgement + strong-model first pass | Maintainer + senior reviewers per category (merge restricted); automated rubric reviewer via `harbor exec` (requires ANTHROPIC_API_KEY, i.e. Claude-family reviewer); `/run` difficulty trials, `/cheat` adversarial trials, `/fortify` hacker-fixer loop | Reviewer-model agreement/accuracy numbers are not published [UNVERIFIED] | https://github.com/harbor-framework/terminal-bench/blob/main/docs/REVIEWING.md ; https://github.com/harbor-framework/terminal-bench/blob/main/docs/TASK_REVIEW_AUTOMATION.md |
| Internet policy: Harbor/TB tasks run WITH open internet by default (`allow_internet=false` is rejected by CI; verifier-side trial-time fetches banned, deps must be baked into images) | Mechanical (platform default + CI check) | Harbor platform + `check-allow-internet` / `check-trial-network-fetch` | N/A (policy, not a judgement) | https://github.com/harbor-framework/terminal-bench/blob/main/docs/TASK_REVIEW_AUTOMATION.md |
| Internet policy: CodeMidas keeps the hidden verifier OUTSIDE the solver environment, injected only at grading (binary execution reward) | Mechanical (isolation architecture) | Pipeline design | N/A; ablation value of cleaning+filtering measured (§5.1 row above) | https://arxiv.org/html/2609.22068v1 (§3) |
| Internet policy during SWE-bench/SWE-Gym/R2E-Gym agent runs | — | — | **No explicit published allow/block rule found in the papers/cards read; treat as [UNVERIFIED].** Containers are per-instance with pre-installed deps (SWE-bench Docker harness; SWE-Gym 6 TB images; SWE-rebench internal PyPI/APT mirrors), which implies setup-time network and need-not-fetch-at-grade-time, but an explicit eval-time egress block is not documented in the sources consulted | https://openai.com/index/introducing-swe-bench-verified/ (Docker harness link); https://arxiv.org/html/2412.21139v2; https://arxiv.org/html/2505.20411v2 (§2.3) |

## 2. Recommended minimal ordered checklist for our pool

Context: tasks have upstream repos + base commits but NO shipped reference solutions, so solvability cannot be
checked against a gold patch unless we construct one (e.g. apply the upstream fix commit). Order is deliberate:
each step is cheaper than the next; never spend judgement (step 5) on a task that fails mechanics (steps 1–4).

1. **Environment builds.** Build the container/image deterministically from pins; record `pip freeze`-style
   lockfiles (SWE-rebench §2.3). Reject unbuildable or non-reproducible envs. (Mechanical.)
2. **Nop fails for the right reason.** Run the hidden tests on the unmodified base commit; require failure,
   and require the failure to be in the tests the instruction points at — not an import error, missing pin, or
   harness crash. Our census (`sound`/`broken_environment`/`grader_suspect` split) already approximates this;
   tighten it to per-test reason codes. (Mechanical; cf. Harbor nop gate.)
3. **The gold/upstream patch passes.** Apply the actual upstream fix (the commit that resolved the issue, or the
   reference diff the task was built from) onto the base commit in a fresh container and require the F2P flip
   plus P2P stability. THIS IS THE MISSING SOLVABILITY CHECK for our pool — nobody has shown a correct fix
   passes the hidden tests. Where no upstream fix exists, a staff engineer must write a reference solution
   (DeepSWE-style: never published upstream) and it must pass. (Mechanical; cf. SWE-bench execution filter,
   SWE-rebench §2.3, CodeMidas §3.3.)
4. **Repeated runs are stable.** Run step 2 twice and step 3 at least 2–4× in fresh containers; reject on any
   flip. This screens flaky tests, which the classic SWE pipelines notably do NOT document filtering for
   (SWE-Gym and SWE-bench publish no rerun-flakiness screen [UNVERIFIED — absence observed in the sources read,
   not proven]). CodeMidas's 2-fail + 4-pass scheme is the concrete template. (Mechanical.)
5. **The instruction specifies what the tests check.** A reader with repo access but no access to hidden tests or
   the fix must be able to reconstruct the graded behavior from the instruction alone (Terminal-Bench's
   "test-instruction alignment" question; OpenAI rubric Q1.1/Q2.1). (Judgement — see §3.)

## 3. The judgement step: what the literature does and how good each is

- **Human panels (gold standard, expensive, imperfect).** OpenAI: 93 devs, 3× labels, max-severity ensemble,
  68.3% rejection rate — but NO published inter-rater agreement, and Verified still needed UTBoost (24.4% of
  leaderboard entries affected) and PatchDiff (+6.2pp inflation) corrections afterward. So: humans-first, but
  humans do not guarantee test adequacy. Terminal-Bench layers senior-reviewers-per-category + restricted merge
  + adversarial `/cheat` trials on top. SWE-bench+: 3 authors, consensus discussion, no kappa.
  Sources: https://openai.com/index/introducing-swe-bench-verified/ ;
  https://arxiv.org/abs/2506.09289 ; https://arxiv.org/abs/2503.15223 ;
  https://github.com/harbor-framework/terminal-bench/blob/main/docs/REVIEWING.md
- **Strong models as reviewers (cheap per-sample, needs calibration).** Terminal-Bench's rubric reviewer (Claude
  via ANTHROPIC_API_KEY), regression-gated at 100% catch on planted-failure tasks; SWE-Dev's Llama-3.1-70B
  patch-vs-reference filter (keeps 65%, parity-or-better downstream); CodeMidas's adversarial-leakage + agreement
  reviewing agents; DeepSWE's independent LLM judge (1.4% disagreement vs 32.4% for inherited tests). No published
  reviewer-vs-human agreement numbers for the TB rubric reviewer [UNVERIFIED].
  Sources: Harbor automation doc + REVIEWING.md (above) ; https://arxiv.org/html/2506.07636v2 ;
  https://arxiv.org/html/2609.22068v1 ; https://arxiv.org/abs/2607.07946
- **Fine-tuned classifiers (best-measured middle ground).** SWE-rebench's Qwen2.5-72B on 3.8k Verified labels:
  81% complexity / 79% clarity / 67% test-correctness — i.e. a cheap-model opinionator like our HAR-112 GLM
  checker (1-in-4 false-"broken", misses ~half) is markedly below what the literature achieves WITH task-specific
  tuning, and even the tuned test-correctness head is the weakest (67%). SWE-smith's difficulty rater: 75.3%.
  Implication for us: replace/augment the generic GLM checker with (a) strong-model rubric review, (b) a head
  fine-tuned on our own blind labels, and (c) mandatory blind-label calibration reporting accuracy/recall, not
  raw "broken" counts.
  Sources: https://arxiv.org/abs/2505.20411 (§2.4, App. F) ; https://arxiv.org/html/2504.21798
- **What nobody in the literature does:** certify soundness from a single cheap model's uncalibrated verdict.
  Every deployed pipeline pairs execution gates (build → nop-fail → gold-pass → stability) with a SEPARATE
  calibrated judgement for specification quality, and reports the judgement's measured error rate.
