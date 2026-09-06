---
source_type: internal
---

# Borrowed Training Stack Scorecard — Track F

Date: 2026-09-03

## Scope and invariant

This scorecard reviews SPADE, two unrelated works called TRACE, Agent Lightning, agent-data-protocol, TRL, and verl against Eval Lab Tracks A–E and defines the smallest credible S0 open-model SFT smoke interface.

Eval Lab remains authoritative for immutable Harbor/ATIF evidence, CAS identity, provenance, admission, redaction, split and cluster isolation, paired experiment plans, and frozen held-out evaluation. It must not become a trainer or a second harness. Mutable-model execution is external and returns a digest-bound result manifest for separate Harbor evaluation.

The required data flow is:

```text
immutable Harbor/ATIF evidence
  -> admissible redacted examples
  -> deterministic capability deficits
  -> quarantined curriculum candidates
  -> paired experiment specs and portable trainer bundles
  -> external trainer result manifest
  -> frozen held-out Harbor evaluation
```

## Decision summary

| Component | Decision | Program boundary |
|---|---|---|
| SPADE | **ADAPT** | Borrow explicit tool-environment and paired hint/no-hint result contracts; render an external-consumer plan only. |
| TRACE-Capability, arXiv:2604.05336 | **ADAPT** | Borrow contrastive deficit metrics and targeted-capability environment-design checks as Tracks B/C methodology. |
| TRACE-Benchmark-Evolution, arXiv:2510.00415 | **ADAPT, optional** | Optional later task-synthesis and validate-by-reproduce reference; never the source of capability-deficit metrics. |
| Agent Lightning | **REJECT for first wave** | Preserve a possible future live-RL plan/result boundary after the SFT-signal gate. |
| agent-data-protocol | **ADAPT** | Use ATIF-v1.7 types and converters as structural references and fixtures, not Track A’s authority or exporter. |
| TRL | **ADOPT as first external trainer target** | Track D renders a pinned SFT plan. GRPO remains disabled until SFT produces held-out signal. |
| verl | **REJECT for v1** | Defer until a measured distributed-rollout requirement exists after SFT works. |

## Explicit two-TRACE disambiguation

### TRACE-Capability

- Paper: *Turning Recurrent Agent failures into Capability-targeted training Environments*.
- arXiv: https://arxiv.org/abs/2604.05336
- Source: https://github.com/ScalingIntelligence/TRACE
- Verified source revision: `d2db23085409555b3f13ea426f42d62cf0bbc43d`.
- Track F role: the Tracks B/C methodology reference for capability deficits and targeted curriculum candidates.
- Exact borrowed concepts: applicability-aware `NA`/`PRESENT`/`LACKING` labels and deterministic post-label metrics `Cov`, `ER-`, `ER+`, and `Delta` from `pipeline/aggregate_capabilities.py::compute_metrics`.

### TRACE-Benchmark-Evolution

- Paper: Guo et al., *Towards Self-Evolving Benchmarks: Synthesizing Agent Trajectories via Test-Time Exploration under Validate-by-Reproduce Paradigm*.
- arXiv: https://arxiv.org/abs/2510.00415
- Published: 2025-10-01.
- Track F role: separate, optional reference for evolving benchmark tasks through proposal mining, problem formation and free exploration, and multi-level validate-by-reproduce checks.

### Disambiguation invariant

Tracks B/C follow **TRACE-Capability (2604.05336)** for `Cov`, `ER-`, `ER+`, `Delta`, and capability-targeted curriculum methodology. **TRACE-Benchmark-Evolution (2510.00415)** may inform an optional later synthesis-validation plan only. It must not replace TRACE-Capability or carry its metric claims. Neither work becomes an evidence authority, executable admission mechanism, trainer, or Harbor replacement. Use the qualified names on every first mention and avoid bare “TRACE” where both could be intended.

## Component scorecards

### SPADE — ADAPT

Pinned source: `spade-rl/spade` version 0.1.0 at `ebd40ec872fc5630cac299cb5e38e7c89743bef5`; MIT; upstream classifies it as Alpha.

Borrow:

- `spade/core/envs/tool_use_base_env.py::ToolUseBaseEnv.reset`, `get_tools`, `execute_tool`, and `step` as an explicit external environment contract: deterministic reset, OpenAI-style JSON-schema tools, action and observation transcript, terminal state, and separately verified success.
- `spade/core/orchestrator.py::_finalize_regrets` only as a result shape: `mean_return_with_hint - mean_return_without_hint` over explicitly paired candidate and seed identities.
- Programmatic environment checks from `spade/core/envs/synthetic_game_env.py::_validate_game_execution` only as criteria to reimplement in a deny-network, no-secret, no-host-write, bounded external sandbox.

Do not borrow:

- `SyntheticGameEnv._load_game_from_code`, which directly executes generated Python with `exec`.
- `EnvironmentValidator.validate_async`, which accepts empty output, a missing verdict, or exceptions as valid.
- SPADE’s evidence model. Its `Trajectory` contains trainer-runtime token IDs, loss masks, and rollout log probabilities; stored Harbor/ATIF evidence must not be coerced into that type.
- Its control plane, model generation, training invocation, or assumptions about OpenRouter/Tinker and multi-GPU mutable policies. API-only ZAI cannot be a weight-updated target.

Track D boundary:

```text
render_spade_consumer_plan(
  bundle: TrainerBundleManifest,
  candidate: QuarantinedCurriculumCandidate,
) -> ExternalTrainerPlan
```

The plan binds input, candidate, environment, split, transform, renderer, model, and tool-schema digests; declares an external sandbox and mutable actor; specifies hinted and unhinted paired rollouts and missingness semantics; and requires a result manifest binding every transcript and return to the supplied identities. Eval Lab neither imports SPADE nor executes the plan.

Kill gate: reject the SPADE-shaped path unless a pinned external sandbox spike (1) deterministically replays admitted content-addressed candidates, (2) preserves declared tool/action/observation/termination semantics, (3) excludes hidden verifier state and hints from no-hint data, (4) reports all missing, timeout, capture-loss, and hint-generation failures without favorable imputation, and (5) returns digest-bound paired results without registering Harbor tasks or changing held-out evaluation.

### TRACE-Capability — ADAPT methodology only

Pinned source: `ScalingIntelligence/TRACE@d2db23085409555b3f13ea426f42d62cf0bbc43d`; paper arXiv:2604.05336; MIT.

Borrow for Track B:

- The contrast between successful and failed evidence for a predeclared capability or mechanism.
- `Cov = lacking_failed / total_failed`.
- `ER- = lacking_failed / (lacking_failed + present_failed)`.
- `ER+ = lacking_passed / (lacking_passed + present_passed)`.
- `Delta = ER- - ER+`.
- The distinction between `NA`, `PRESENT`, and `LACKING`, including the warning that false `LACKING` judgments inflate `Cov` and `Delta`.

These metrics are deterministic only after labels exist. In Eval Lab they may summarize certified Harbor/ATIF facts and versioned deterministic rules; they are evidence-conditioned prioritization, not proof of a causal or inherent model deficit. Model-proposed labels remain unclassified until certified.

Borrow for Track C:

- Target one certified capability with attributable reward.
- Preserve relevant interaction shapes rather than copying or wrapping the original simulator.
- State transfer rationale, controls and twins, hidden-verifier plan, solvability plan, resource limits, and leak scan.
- Reject candidates whose success can be explained by an unrelated shortcut.
- Treat rollout reward distribution, pass@k, and within-group variance as later validation plans, not candidate-generation proof.

Do not adopt TRACE-Capability’s LLM discovery/labeling pipeline, Markdown-agent workflow, executable `GameSpec` registry, generated Python environments, vLLM collector, GRPO/LoRA loop, or MoE router.

Kill gate: do not escalate this deficit-to-candidate line to RL unless an approved external SFT intervention produces a predeclared improvement for at least one certified mechanism family on complete interleaved, cluster-disjoint frozen Harbor pairs with no capture loss. Null, negative, pooled-only, incomplete, or contaminated evidence fails.

### TRACE-Benchmark-Evolution — ADAPT as optional synthesis reference

Source: Guo et al., arXiv:2510.00415.

The framework evolves existing benchmark tasks through evolutionary proposal mining, problem formation and free exploration, and multi-level validate-by-reproduce checks. It may later inform a quarantined candidate-synthesis validation checklist. It does not define `Cov`, `ER-`, `ER+`, or `Delta`; it is not the Track B deficit-extraction source; and it cannot replace Eval Lab admission, provenance, leak controls, or held-out evaluation.

Kill gate: any future adaptation must produce only quarantined specifications with immutable parent identity and an independently reproducible validation plan. If it requires registering evolved tasks, executing untrusted candidate code, or treating a generated trajectory as admitted evidence before Eval Lab policy gates, reject it.

### Agent Lightning — REJECT for first wave

Pinned source: `microsoft/agent-lightning` v1.0.1, main revision `218f1f7c0bac0800de4d5a4e5e6f61cf7b5038b4`; MIT.

Verified execution surface:

- `agentlightning/verl/entrypoint.py::run_ppo` requires non-empty in-memory train and validation sequences, initializes Ray, and launches a customized VERL PPO path. The v1 tree exposes no SFT runner.
- `agentlightning/server/proxy.py::ProxyRouter.prepare_body` asks a live OpenAI-compatible model endpoint for token IDs and optionally log probabilities.
- `agentlightning/verl/agl_rollout_manager.py::AglRolloutManagerBase._build_completed_rollout` consumes Gateway events and assigns only the last scalar reward event as final reward.
- Missing reward is replaced by configurable `reward_fillna_value`, defaulting to `0.0`.
- `agentlightning/server/store.py` uses process-memory dictionaries and lists, with no CAS, durable provenance, or evidence contract.
- The trainer stack requires Python >=3.12 and tightly coupled CUDA 12.9/13, VERL `<0.9`, Ray, vLLM, PyTorch, and FlashAttention components.

This is live PPO-family orchestration, not a consumer of static ATIF trajectories or SFT prompt/response examples. Do not replay ATIF through its event store, redirect Harbor through its Gateway, or treat Gateway rewards as evidence.

Possible future boundary:

```text
render_agent_lightning_plan(bundle, requirements) -> ExternalTrainerPlan
consume_agent_lightning_result(manifest, expected_bundle_digest) -> VerifiedExternalResult
```

External rows would contain only admitted task inputs and stable `data_id` linkage. The runner would own the Gateway, Controller, agent process, model server, verifier integration, token/logprob production, and result-manifest wrapper.

Kill gate: reject Agent Lightning unless a pinned isolated Linux-GPU spike preserves bundle, split, model, config, rollout, verifier, and checkpoint digests in a result manifest and permits a subsequent independent frozen Harbor evaluation without Gateway state. Any requirement to reroute Harbor, replay static ATIF as live rollout truth, or accept semantically unsupported or missing rewards fails.

### agent-data-protocol — ADAPT structural reference

Pinned source: `neulab/agent-data-protocol@040a279b46b2388ae42b43449f8645b9781c7bf7`; package version 0.0.0; ATIF schema `ATIF-v1.7`. The README states MIT, but the inspected root has no tracked license text; dataset licenses remain separate.

Useful source surfaces:

- `schema/atif.py::{ATIFTrajectory, Step, ToolCall, ATIFObservation, ObservationResult, ContentPart, content_to_text}` for structural validation and reference fixtures.
- `normalize_atif_trajectory` only behind a versioned Eval Lab derived-view adapter. It deep-copies but rewrites duplicate call IDs, maps tool names and arguments, and extracts `<think>` blocks. Preserve immutable raw evidence and record raw digest, adapter identity/version, and derived digest.

Do not directly use:

- `scripts/atif_input.py::load_trajectory`, because it drops system steps and moves arbitrary `extra` into untyped details.
- `agents/openhands_sdk/std_to_sft.py::process_trajectory` or the SWE-agent converter as portable exports. Their formats are agent-specific and lack source-artifact digest, trial/job identity, admission result, split, cluster, redaction, and typed exclusion fields.

ADP contains no verified hidden-verifier, secret, PII, or redaction scanner and no Eval Lab admission, cluster isolation, provenance, digest, or typed exclusion contract. Its arbitrary text, tool arguments, observations, `reasoning_content`, `extra`, metrics, and media references must be scanned before export.

Kill gate: a fixture containing hidden-verifier content, a secret, or missing immutable lineage must produce only a typed exclusion. Every allowed output must bind its source artifact digest, cluster and split, admission decision, extractor/normalizer version, and redaction-report digest. Any violating SFT or episode row rejects the adapter.

### TRL — ADOPT as first external trainer target

Pinned source: `huggingface/trl@312727b3ef44400e60032be1122fbce7865ff24d`; Apache-2.0; inspected source version 1.13.0.dev0; Python >=3.10; dependencies include `transformers>=4.56.2`, `accelerate>=1.4.0`, and `datasets>=4.7.0`.

SFT surface:

- `trl.SFTTrainer` and `trl.SFTConfig` accept standard or conversational language-modeling and prompt-completion records.
- `trl/data_utils.py::{is_conversational, apply_chat_template, maybe_apply_chat_template}` render processor-dependent chat records. The canonical Track A/D bundle therefore keeps unrendered messages and pins processor/tokenizer and template identity, revision, and digest.
- `assistant_only_loss=True` requires a template capable of returning assistant masks. TRL can patch known families with generation-tagged training templates and raises if an example has no assistant tokens.
- `completion_only_loss=True` applies to prompt-completion records.
- Any dataset-supplied `labels` column bypasses mask generation and is prohibited in S0 input.
- Tokenization uses `add_special_tokens=False` after chat-template rendering to avoid double BOS and truncates to `max_length`; Eval Lab must refuse semantically important truncation before submission.
- QLoRA uses `BitsAndBytesConfig(load_in_4bit=True, ...)` plus `peft.LoraConfig`. Quantization configuration must appear in exactly one supported location.

Later GRPO surface:

- `trl.GRPOTrainer` requires prompts, generates fresh on-policy completions, and calls a reward function shaped like `reward_func(prompts, completions, completion_ids, **allowlisted_columns) -> list[float]`.
- Recorded ATIF token IDs and log probabilities are not GRPO inputs. Hidden or evaluator-only fields must never travel through retained reward columns.

TRL checkpoints and model cards are attachments, not Eval Lab result manifests. An external wrapper must bind inputs, effective configuration, model/tokenizer/template/checkpoint artifacts, logs and metrics, runtime identity, terminal status, and the separate Harbor evaluation handoff.

Kill gate: the TRL path must pass a pinned external Linux-GPU SFT smoke that consumes one Track-D-rendered redacted bundle, completes a bounded update, and emits the required digest-bound manifest without any hidden field entering materialized data. Failure to preserve the boundary rejects TRL for the program.

### verl — REJECT for v1

Pinned source: `volcengine/verl@e42d6af6e85dd37c907af5ea99326355c376bd97`; version 0.10.0.dev; Apache-2.0.

Potential later surfaces:

- `verl/trainer/main_ppo.py` is a Hydra PPO entry point.
- `verl/experimental/reward_loop/reward_manager/naive.py` consumes a function shaped like `compute_score(data_source, solution_str, ground_truth, extra_info) -> float | {"score": ...}`.
- `verl/utils/dataset/multiturn_sft_dataset.py::MultiTurnSFTDataset` consumes parquet rows with `messages`, optional `tools`, and thinking/media fields.
- RL rows use prompt, `data_source`, `reward_model.ground_truth`, and allowlisted `extra_info`.

Current incompatibilities:

- Ray >=2.41 plus Hydra, vLLM or SGLang, pinned Torch/CUDA combinations, and a documented quickstart minimum of 24 GB HBM.
- No canonical machine-readable result manifest; metrics go to pluggable loggers and checkpoints to step directories.
- Moving or tightly pinned dependencies and mutually exclusive backend extras.
- It duplicates later RL execution surfaces before the SFT signal exists.

Do not render or implement a verl adapter in v1. Preserve backend neutrality. Reconsider only after a demonstrated distributed-rollout bottleneck. A future pure renderer must produce byte-identical Hydra/config and reward-shim plans without importing verl, Torch, or Ray and must map every field to a pinned documented schema.

## Minimal cross-track stable interfaces

### A. `TrainingDatasetManifest` and JSONL records

Manifest fields:

- `schema_version`.
- Source trial, job, and immutable artifact identities and digests.
- Extractor and normalizer identity, version, and derived digest.
- Benchmark and task family.
- Content identity for deduplication.
- `cluster_key` and split.
- Capture, environment-integrity, and evaluator status.
- Admission outcome and typed exclusion reasons.
- Redaction report and digest.
- Aggregate included/excluded counts.

SFT record:

```text
SFTExample {
  example_id,
  messages | (prompt, completion),
  tools?,
  lineage_ref
}
```

Episode/step records remain evidence-shaped and tokenizer-independent. Canonical records omit tokenizer IDs, loss masks, per-token probabilities and log probabilities, trainer rewards, reference-model fields, and backend configuration.

### B. `CapabilityDeficitArtifact`

```text
CapabilityDeficitArtifact {
  schema_version,
  artifact_digest,
  source_evidence_digests,
  mechanism_family,
  deterministic_rule_id_and_version,
  supporting_evidence_ids,
  counterevidence_ids,
  applicability_and_outcome_counts,
  optional_cov_er_minus_er_plus_delta,
  classification_and_confidence_boundary,
  split_and_cluster_facts
}
```

It cannot claim causality, an inherent capability deficit, or transfer.

### C. `QuarantinedCurriculumCandidate`

```text
QuarantinedCurriculumCandidate {
  schema_version,
  certified_deficit_ref,
  deterministic_transform_and_seed,
  candidate_spec_digest,
  cluster_and_twin_identity,
  target_capability,
  controls,
  hidden_verifier_plan,
  solvability_plan,
  leak_scan_plan,
  resource_and_sandbox_requirements,
  later_admission_plan
}
```

It contains no execution or registration side effect and is not admitted training data.

### D. `TrainerBundleManifest`

```text
TrainerBundleManifest {
  schema_version,
  bundle_digest,
  objective,                    # sft first; later verifier_reward_episode
  model_or_checkpoint_locator_and_digest,
  train_and_eval_dataset_digests,
  split_exclusion_proof,
  renderer_id_and_version,
  tokenizer_processor_template_identity_revision_digest,
  template_kwargs,
  eos_and_pad_policy,
  max_length_and_truncation_policy,
  loss_scope,
  seed,
  backend_requirements,
  typed_incompatibilities,
  expected_result_schema
}
```

Pure renderers may produce a TRL plan and an experimental SPADE-shaped external-consumer plan. They do not import trainer libraries or launch processes.

### E. `ExternalTrainerResultManifest`

```text
ExternalTrainerResultManifest {
  schema_version,
  backend_and_version,
  upstream_source_revision,
  execution_image_digest,
  submitted_bundle_digest,
  resolved_model_tokenizer_template_digests,
  canonical_effective_config,
  materialized_input_digests,
  seed,
  runtime_and_hardware_identity,
  terminal_status_and_failure,
  metric_and_log_digests,
  checkpoint_model_tokenizer_config_artifact_uris_and_digests,
  verifier_or_reward_contract_digest?,
  held_out_result_included: false
}
```

Frozen Harbor evaluation is separate. Its request and result may reference this manifest but are not trainer-owned acceptance evidence.

## Global SFT-before-RL gate

All GRPO, PPO, SPADE, Agent Lightning, and verl paths must refuse with `SFT_SIGNAL_NOT_ESTABLISHED` unless a validated signal record binds:

1. Exact SFT bundle and produced checkpoint digests.
2. Frozen, cluster-disjoint Harbor held-out evaluation identity and digest.
3. Complete pair and capture accounting where paired.
4. A predeclared mechanism/class-specific success and regression decision.

An absent, mismatched, incomplete, pooled-only, or failing record blocks RL deterministically. Training loss alone is not signal.

## S0: smallest credible open-model SFT smoke interface

S0 is an interface and dry plan, not a training run.

### Model families

#### Primary: Qwen3

- Model: `Qwen/Qwen3-0.6B`.
- License: Apache-2.0.
- Source model repository: https://huggingface.co/Qwen/Qwen3-0.6B
- Transformers floor from the model card: 4.51.0; the pinned TRL source already requires a higher Transformers version.
- Verified model facts: 0.6B parameters, BF16 config, tied embeddings, `max_position_embeddings=40960`, EOS `<|im_end|>` with token ID 151645.
- Stock template accepts standard `tools`, OpenAI-style assistant `tool_calls[].function.{name, arguments}`, tool-response turns, `reasoning_content`, and `enable_thinking`.
- TRL documentation uses this exact model in the SFT quick start and documents automatic training-template patching for known Qwen3 families when assistant-only loss is enabled.

Use `enable_thinking=false` for the smoke so targets are short and deterministic. Verify the patched generation mask rather than trusting family recognition.

#### Optional second-family conformance: SmolLM3

- Model: `HuggingFaceTB/SmolLM3-3B`.
- License: Apache-2.0.
- Source model repository: https://huggingface.co/HuggingFaceTB/SmolLM3-3B
- Transformers floor: 4.53.0.
- Its template includes generation markers for assistant-only loss, but tool schemas use the non-standard `xml_tools` argument rather than the standard `tools` argument.

Drop this branch rather than maintaining a custom template if the `tools -> xml_tools` projection is not a pure, lossless renderer mapping. Qwen3 alone is sufficient for S0.

### Backend

External TRL `SFTTrainer` plus PEFT. Use QLoRA only as an execution choice, not a canonical data property. Eval Lab emits the plan and expected result schema; no trainer or model is imported or run inside the repo.

### S0 record and rendering contract

Every example uses one uniform format.

Recommended conversational record:

```text
{
  messages: [
    { role: system | user | assistant | tool, content: string, ... },
    ...
  ],
  tools?: [
    {
      type: "function",
      function: {
        name: string,
        description: string,
        parameters: JSONSchema
      }
    }
  ]
}
```

Assistant tool calls use `tool_calls[].function.{name, arguments}`. Tool results preserve the corresponding call identity where the normalized schema requires it. All content remains lineage-bound through the sibling manifest.

Pinned rendering fields:

- Model, tokenizer or processor, and chat-template revision and digest.
- `enable_thinking=false`.
- EOS and pad-token policy.
- `max_length` approximately 4096 for S0.
- Export truncation policy `error`; do not silently truncate terminal assistant, tool, or verifier-relevant spans.
- `assistant_only_loss=true` for conversational data.
- `packing=false`.
- Deterministic seed.
- `trust_remote_code=false` unless separately admitted.

Prompt-completion is allowed as a separate uniform bundle with `completion_only_loss=true`, but must not be mixed with conversational semantics in one smoke.

Explicitly absent from S0 inputs:

- Token IDs or precomputed `labels`.
- Per-token probabilities or log probabilities.
- Rollout or reward fields.
- Reference-model or KL configuration.
- vLLM and RL configuration.
- Hidden verifier, evaluator-only, secret, or prohibited-corpus material.

SFT is teacher-forced NLL computed inside the external trainer. Log probabilities are not inputs.

### GPU class and external prerequisites

GPU memory figures are engineering estimates, not measured source facts:

- Qwen3-0.6B QLoRA: approximately 8–16 GB; 24 GB is comfortable.
- Qwen3-0.6B full BF16 SFT: approximately 12–16 GB including activations under a short sequence smoke; declare one 24 GB CUDA GPU as the conservative S0 class.
- SmolLM3-3B QLoRA: approximately 16–24 GB.
- SmolLM3-3B full SFT: approximately 36–48 GB plus activations and is not S0-scale.

Repository readiness means manifests, schemas, template fixtures, and dry plans validate without a GPU. External prerequisites remain:

- Linux and a compatible CUDA GPU.
- Pinned model and tokenizer availability and immutable revisions.
- PyTorch, Accelerate, Transformers, TRL, and PEFT compatibility.
- `bitsandbytes` compatibility if QLoRA is selected.
- Storage, network/cache, licensing, and actual GPU scheduling.

No external GPU environment is configured tonight.

### S0 exact kill evidence

The dry interface is ready only if all of the following hold:

1. Every record passes lineage, admission, redaction, prohibited-corpus, hidden-verifier, and secret gates; train and held-out clusters do not overlap.
2. The pinned tokenizer/template renders every fixture deterministically and visibly embeds the declared tool schemas.
3. Tool calls and tool results round-trip without losing roles, names, JSON arguments, or call identities.
4. Assistant masks contain at least one target token per example, cover only intended assistant targets, include the terminal EOS/EOT token, and never cover system, user, or tool observations.
5. Any preexisting `labels` column is rejected.
6. Tokenized length distribution and p99 are reported. Any terminal assistant/tool or semantically relevant truncation kills the plan.
7. Model, tokenizer, template, data, and effective configuration revisions are digest-bound.
8. PEFT and quantization configuration is schema-valid and quantization configuration is supplied in exactly one supported place.

Later external execution fails S0 if any of these occurs:

- Template or dataset-map exception.
- Assistant-mask exception or an empty assistant target.
- Non-finite loss at the first optimizer step.
- OOM on the declared GPU class and sequence budget.
- Failure to emit the required external result manifest.
- Any hidden or evaluator-only field entering materialized training data.

A green dry interface is not training evidence. A successful bounded update is not held-out behavioral evidence. No RL path opens until the global SFT-signal gate passes.

## Final recommendation

Implement only the backend-neutral artifacts, a pure TRL SFT plan renderer, and a strictly external SPADE-shaped plan contract. Use TRACE-Capability and agent-data-protocol as methodology/schema references. Keep TRACE-Benchmark-Evolution explicitly separate and optional. Reject Agent Lightning and verl from the first wave until held-out SFT signal and a concrete live-RL or distributed-scale requirement justify their operational cost.
