---
source_type: internal
---

# verl second-wave reopening criteria — system-design probe

Date: 2026-09-03  
Owner: `wK:p9 (GPT-5.6 Sol)`  
Decision: **KEEP CLOSED / REJECT.** `verl` is not reopened for v1. A later reopening authorizes only a bounded external spike; it does not adopt `verl` or add it to Eval Lab's runtime.

## Evidence anchors

All claims below are anchored to these exact artifacts:

- `research/inbox/borrowed-training-stack-scorecard-20260903.md`, SHA-256 `30347c83afe8a6b3b1fd105b21f9564f744df7463b99b67b719888482a473724`: `verl` rejection and incompatibilities at lines 191–209; global SFT-before-RL gate at 331–340; S0 kill evidence at 445–467.
- `research/inbox/track-h-composition-ruling-20260903.md`, SHA-256 `cb73fac5efd8b0a8d9d6f2799a78d9c9685c28649d50a30a1dc54f2dab464bed`: v1 admits TRL/SFT only and refuses RL/SPADE/verl until a bytes-verified SFT result exists at lines 22–34.
- `research/inbox/RESEARCHER-SYSTEM-BLUEPRINT-2026-09-03.md`, SHA-256 `4a3c06b3148f0edb3eba9df9da6dde3830e63c72d06e41883036c19e10f80b17`: S0 must prove held-out SFT signal before the S1 Linux-GPU RL stage at lines 61–66.
- `src/evallab/trainer_bundle.py` at integration `6ede71a0`, file SHA-256 `b7bdb9fd6de7f16d521a7649cfb36053ab1fdfa6ae70f302c0da6a9b437078d3`: current authority scope is fixture-only/copied-digest-refs and real-corpus training is false at lines 242–244 and 693–695; `SFT_SIGNAL_NOT_ESTABLISHED` is typed at 317 and enforced by `backend_incompatibilities` at 785–803.
- `src/evallab/training_result.py` at integration `6ede71a0`, file SHA-256 `74152accffd416c59bfa421038e8b575c9d7cb1655a8da6585da555943800bf9`: the external result manifest contract is at lines 321–423 and requires completed-run receipts and non-contamination evidence at 406–410.

## What “reopen” means

Reopening means approving one time-boxed, disposable integration spike with:

1. a pure, deterministic plan renderer inside Eval Lab;
2. an external pinned `verl` image/runtime outside Eval Lab;
3. the existing digest-bound trainer result and separate Harbor evaluation boundaries.

It does **not** mean importing `verl`, Ray, Torch, vLLM, SGLang, or trainer execution into Eval Lab. It does not make `verl` the control plane, mutate Harbor, or grant held-out data to the trainer.

## Reopening gate — all four conditions are conjunctive

### R1. SFT signal is established

A bytes-verified completed SFT result must bind the exact training bundle, input and produced checkpoint, effective configuration, runtime receipts, and frozen cluster-disjoint Harbor result. The predeclared mechanism/class-specific held-out success threshold must pass, with complete pair/capture accounting where applicable. Training loss, a completed optimizer step, or a green dry renderer is not signal.

**Current state:** unsatisfied. Track D remains fixture-only and the global typed `SFT_SIGNAL_NOT_ESTABLISHED` refusal is correct.

### R2. A concrete on-policy workload exists

A backend-neutral `verifier_reward_episode` plan must name a pinned checkpoint, task set, verifier/reward contract, rollout count, maximum sequence budget, concurrency, seed policy, terminal-status semantics, and external result contract. Reward inputs must exclude evaluator-only and held-out fields. “We want RL,” framework popularity, or available GPUs are not workloads.

### R3. A measured single-node bottleneck exists

Run the exact R2 workload through the simplest valid single-node external baseline first. A `verl` spike may open only if either:

- **capacity case:** the minimum scientifically useful model/context/rollout batch cannot fit the declared single-node GPU class after the already-approved memory controls; or
- **throughput case:** the baseline misses a predeclared experiment-cycle SLA by at least `2×`, and profiling attributes at least `60%` of end-to-end step time to rollout generation/coordination that can actually be parallelized.

The `2×` and `60%` values are decision thresholds, not empirical claims. Record raw timings, accepted episode count, hardware identity, configuration digest, and profiler artifact digest. A slow tokenizer, verifier, archive writer, or evaluator does not establish a distributed-rollout bottleneck.

### R4. The authority boundary is production-capable

Before real data reaches any spike, Track D must replace its current fixture-only/copied-digest-ref authority status with reverified bytes-level authorities and an affirmative non-caller-settable real-corpus policy decision. The pure renderer must map every emitted field to a pinned documented `verl` schema while preserving exact source, reward, model, tokenizer, template, split, and result identities.

If any R1–R4 condition is absent, the decision remains **REJECT** and no renderer is added.

## Spike acceptance and falsifiable kill gate

A reopened spike becomes an adoptable external backend only if one immutable evidence bundle demonstrates every item below:

1. **Determinism:** two renders of the same inputs are byte-identical; plan and effective-config digests match.
2. **Boundary purity:** importing the renderer does not import or initialize `verl`, Ray, Torch, vLLM, SGLang, CUDA, network clients, or subprocess execution.
3. **Schema completeness:** every Hydra/config/reward-shim field maps to the pinned upstream revision; unknown, default-only, or silently dropped fields refuse.
4. **Authority:** all dataset, checkpoint, verifier, and reward inputs reverify from durable authorities; no caller boolean or copied digest authorizes training.
5. **Semantic parity:** a fixed fixture yields exact prompt/tool-call/tool-result identities, terminal status, scalar reward, and reward components across the baseline and `verl` path.
6. **Isolation:** the trainer receives zero frozen held-out examples, evaluator-only fields, hidden verifier material, or non-contamination evidence inputs.
7. **Result loadability:** completed, failed, and interrupted executions emit the existing `TrainerResultManifest`; completed results include receipts, logs, checkpoint artifacts, and non-contamination evidence.
8. **Distributed value:** from one to two equivalent workers, accepted episodes/hour improves by at least `1.6×` (80% parallel efficiency) at no more than `1.25×` baseline cost per accepted episode, **or** the capacity case executes the previously impossible minimum workload. These are predeclared policy thresholds.
9. **Failure recovery:** an injected worker loss produces no duplicated/lost accepted episode identity and cannot publish a completed result; retry/recovery remains external and digest-bound.
10. **Evaluation separation:** any behavioral improvement claim comes only from the separately run frozen Harbor evaluation defined in R1, never trainer metrics.

**Kill rule:** one failed item closes the `verl` path and leaves no compatibility alias, fallback, optional import, queue branch, or half-supported config in Eval Lab. Reconsider only after a material change in the pinned upstream revision, workload, authority contract, or hardware class; do not tune the gate after seeing the result.

## Rejected alternatives

1. **Render a `verl` plan now for future-proofing — rejected.** Without R1–R3, this creates a second unexercised schema against a moving upstream API and violates the scorecard's explicit v1 boundary.
2. **Adopt `verl` because distributed training is generally desirable — rejected.** Scale is not evidence of a bottleneck; the measured capacity/throughput gate decides.
3. **Build an Eval Lab Ray/vLLM rollout scheduler — rejected permanently.** It would make the lab a trainer/control plane and duplicate borrowed execution surfaces.
4. **Use Agent Lightning as the intermediate bridge — rejected for this decision.** The scorecard records a live PPO stack with in-memory stores, gateway reward semantics, and tighter CUDA/runtime coupling rather than the required durable evidence boundary.
5. **Keep TRL no matter what — rejected as a universal rule.** TRL remains the smallest first external target, but a passed R1–R4 plus spike gate is legitimate evidence to add `verl` externally.
6. **Treat multi-GPU capacity alone as adoption — rejected.** Capacity can authorize a spike, but adoption still requires authority, semantic parity, result loadability, isolation, and failure behavior.

## Decision procedure

```text
SFT signal absent?                         -> KEEP CLOSED
No exact verifier-reward workload?        -> KEEP CLOSED
No measured single-node rollout limit?    -> KEEP CLOSED
Track D authority still fixture-only?     -> KEEP CLOSED
All R1-R4 pass?                            -> ALLOW ONE EXTERNAL SPIKE
Any spike kill item fails?                -> CLOSE, REMOVE SPIKE SURFACE
All ten spike items pass?                  -> ADOPT AS OPTIONAL EXTERNAL BACKEND
```

No implementation change is warranted by this probe.
