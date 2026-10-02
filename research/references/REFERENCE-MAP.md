# Agent monitoring, reward integrity, and task validity

**HAR-160 · 2026-10-02 · $0 research · Eval Lab source: `ee6e130d`.** Published mechanisms, not reproduced results. Collection ≠ judgment ≠ intervention. A failed rollout does not prove an unsolvable task; an LLM flag is not a confirmed exploit.

## 1. Live monitoring and observability

Semantic monitors (Scout, Docent, Ari) are in §5; these are collection/integration references.

| Reference | What it does | What we should take | Link / section |
|---|---|---|---|
| Inspect bridge/sandboxes | Proxies sandbox model calls; explicitly grants provider-hosted capabilities. | Capture routed calls; sandbox egress alone does not constrain provider-hosted tools. | [Sandbox Bridge; Granted Capabilities][inspect] |
| OpenTelemetry GenAI | Inference/tool spans, usage, errors; content capture opt-in; conventions still Development. | Version-pin a redacted projection, not another canonical store. | [gen-ai-spans.md][otel] |
| LangSmith / Langfuse | OTLP ingestion with separately configured evaluations. | Portable exports; verify ingestion freshness before calling it live monitoring. | [LangSmith tracing][langsmith]; [Langfuse real-time ingestion][langfuse] |
| Phoenix / W&B Weave | Phoenix traces/annotations; Weave instrumented calls and scorers. | Source-linked annotations; a viewer/scorer is not a validated hack detector. | [Phoenix Features][phoenix]; [Weave automatic tracking][weave] |

## 2. Reward-hack detection and contamination

| Reference | What it does | What we should take | Link / section |
|---|---|---|---|
| MiMo-V2.6 report | Scrubs artifacts/caches/Git; isolates networking; iterates hack-agent probes and offline audits. | Red-team before training; preserve raw reward separately from confirmed-hack correction. | [§4.2.6, pp14–16][mimo-hack] |
| GLM-5.2 / mimoagent | GLM describes rules + intent judge, blocking calls while continuing rollouts. MiMo code ships regex-only; its LLM confirmation is a stub. | Copy interception and operator logs, not claimed two-stage accuracy; calibrate legitimate-command negatives. | [GLM Anti-Hack][glm]; [antihack.py, confirm/_llm_judge][antihack] |
| METR | Anomalous-score and LLM screens followed by manual review; each screen misses cases. | Audit non-flags too; test grader/timer tampering, not only upstream fetching. | [How did we find these examples?][metr] |
| SWE-bench contamination audit | Elicits task/gold-patch recall, checks probe leakage, manually reviews strong cases. | Separate pretraining exposure from runtime answer leakage; “Verified” is not decontamination. | [2026 audit: contamination probes][swe-contamination] |
| SWE-smith | Test-breaking mutations in prepared repos; excludes SWE-bench repositories. | Repository-disjoint splits and fail-to-pass controls; synthetic tasks are not universal realism. | [§2.1; dataset construction][swesmith] |

## 3. Task/environment validation

| Reference | What it does | What we should take | Link / section |
|---|---|---|---|
| MiMo-V2.6 | Reference-patch test flips stable over eight reruns; four-rollout specification/test audit. | Accept alternative correct implementations; auditor/reward disagreement requires review, not automatic invalidation. | [§4.2.1, pp9–11][mimo-tasks] |
| SWE-Gym / R2E-Gym | Executable environments and reference-patch/test validation; R2E synthesizes tests/specifications. | Bind checks to exact environment/task versions; execution and semantic correctness need separate evidence. | [SWE-Gym §3.1][swegym]; [R2E §2][r2e] |
| SWE-rebench | Failing tests turn passing; prior passing tests stay passing; quality labels and dated slices. | Pin dependencies; treat automated clarity/test-quality labels as fallible metadata. | [§§2.2–2.4, 3.2][rebench] |
| DeepSWE (Datacurve) | Original tasks, behavioral verifiers, shallow clones, three-run flake checks, human QA. | Prompt–test alignment and acceptance breadth. Judge disagreement is not ground-truth error. | [§§3.3–3.4, 4.2–4.3][deepswe] |
| Nemotron SWE | OpenHands trajectories sourced from SWE-Gym/R2E-Gym prompts. | Preserve source-task lineage; trajectory volume is not an environment-validity certificate. | [Dataset Description / References][nemotron] |

## 4. GEPA / DSPy for tasks and data

| Reference | What it does | What we should take | Link / section |
|---|---|---|---|
| GEPA | Optimizes text against evaluator scores and diagnostic feedback. | Propose instruction/rubric repairs; gate on unchanged requirements and controls, not higher pass rate alone. | [writing_evaluators.md: contract, gated objective][gepa] |
| DSPy / Dropbox | Optimizes judge instructions against human ratings; Dropbox constrains edits and penalizes malformed outputs. | Frozen train/validation/test splits; preserve label scale and prohibit copied examples; calibrate before filtering data. | [Choosing training/validation sets][dspy]; [Dropbox: human agreement, guardrails][dropbox] |
| gskill | SWE-smith tasks drive GEPA skill optimization with held-out testing. | Reuse the data→feedback→artifact loop; skill gains do not validate repaired tasks. | [Recipe / Experiments][gskill] |

## 5. Trace-analysis agents and measured accuracy

| Reference | What it does | What we should take | Link / section |
|---|---|---|---|
| Inspect Scout | Regex/LLM scanners; labeled validation reports precision, recall, F1 and balanced accuracy. | Freeze dev/test labels; measure each scanner on our corpus. | [Validation Results / Splits][scout] |
| Docent | Deterministic DQL selection, model Readings, clustering, structured human labels. | Cite per-run evidence before aggregation; measure held-out agreement, not judge confidence. | [Analysis Plans][docent]; [Labeling][docent-labels] |
| Applied Compute Ari | Samples traces after training steps; calibrated judge triggers wider investigation, including non-flags. | Human-reviewed hard negatives; low-FPR test evaluation. Reported 2% FPR is not 98% precision or transferable accuracy. | [How detection works / Benchmarking][ari] |
| Who&When | Whole-log/stepwise/search raters compared with expert failure labels; reported best exact-step accuracy 14.2%. | Measure step windows separately; failure-only data cannot establish benign FPR. | [§§3.2, 4; abstract][who] |

## Our top five changes — proposed, in priority order

1. **Strengthen validity packets, not “all-fail” filtering.** Extend [existing stages](../../docs/task-quality.md) with requirement→assertion mapping, a passing witness, wrong-solution negatives and stability receipts. Missing witness stays unresolved; task repairs create new versions.
2. **Make exploit probes regression cases.** Extend `mimo_exploit.py` and the existing hack probe with MiMo/METR leak and grader-tampering cases plus benign counterexamples. Keep suspected, blocked, acquired and confirmed outcomes distinct; never let a classifier rewrite raw reward.
3. **Close capture/telemetry joins.** Extend [model capture](../../docs/model-capture.md) and `run_telemetry.py` with stable trial/request links, auxiliary-call coverage and measured request timing. Acceptance: missing calls remain visible; step gaps are not advertised as server latency.
4. **Populate held-out monitor evidence.** Use [existing investigation scoring](../../docs/monitor-investigations.md), not another rater service: unseen task families, random non-flags and hard negatives; publish per-label precision/recall, low-FPR recall, intervals, abstention and coverage. Keep proposed interventions approval-only.
5. **Constrain optimization and finish counts adoption.** `gepa_optimizer/evaluator.py` already supports `COUNTED_VERDICT`; retire its legacy opt-in `UPSTREAM_FETCH_ZERO` when migrating callers. Score repair candidates against frozen human labels/control packets; reject requirement drift and held-out leakage.

[inspect]: https://inspect.aisi.org.uk/agent-bridge.html#sandbox-bridge
[otel]: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md
[langsmith]: https://docs.langchain.com/langsmith/trace-with-opentelemetry#how-otel-tracing-works
[langfuse]: https://langfuse.com/integrations/native/opentelemetry#real-time-ingestion
[phoenix]: https://arize.com/docs/phoenix/tracing/llm-traces#features
[weave]: https://docs.wandb.ai/weave/guides/tracking/create-call#automatic-tracking-of-llm-library-calls
[mimo-hack]: https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Flash-RL/resolve/main/MiMo_V2_6_technical_report.pdf#page=14
[glm]: https://z.ai/blog/glm-5.2
[antihack]: https://github.com/XiaomiMiMo/mimoagent/blob/467f0a19016f0ac4d63b8d17a1f0da9ba07f232c/src/mimoagent/agents/antihack.py#L319-L346
[metr]: https://metr.org/blog/2025-06-05-recent-reward-hacking/
[swe-contamination]: https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/
[swesmith]: https://arxiv.org/html/2504.21798v2#S2.SS1
[mimo-tasks]: https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Flash-RL/resolve/main/MiMo_V2_6_technical_report.pdf#page=9
[swegym]: https://arxiv.org/html/2412.21139v2#S3
[r2e]: https://arxiv.org/html/2504.07164v1#S2
[rebench]: https://arxiv.org/html/2505.20411v1#S2.SS3
[deepswe]: https://arxiv.org/html/2607.07946v1#S4
[nemotron]: https://huggingface.co/datasets/nvidia/Nemotron-SWE-v1
[gepa]: https://github.com/gepa-ai/gepa/blob/main/.claude/skills/gepa-optimize-anything/references/writing_evaluators.md
[dspy]: https://dspy.ai/current/getting-started/gepa-optimization/#choosing-the-training-and-validation-sets
[dropbox]: https://dropbox.tech/machine-learning/optimizing-dropbox-dash-relevance-judge-with-dspy
[gskill]: https://gepa-ai.github.io/gepa/blog/2026/02/18/automatically-learning-skills-for-coding-agents/#experiments
[scout]: https://meridianlabs-ai.github.io/inspect_scout/validation.html#validation-results
[docent]: https://docs.transluce.org/analysis/analysis-plans#common-use-cases
[docent-labels]: https://docs.transluce.org/analysis/labeling
[ari]: https://www.appliedcompute.com/platform/reward-hacking-detection-in-ari#benchmarking-monitor-performance
[who]: https://arxiv.org/html/2505.00212v3#S3.SS2
