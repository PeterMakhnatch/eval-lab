# Tiny-scale rejection-sampling SFT for the overnight Python-agent experiment

Date: 2026-10-01. Owner: Library, [HAR-134][har134]. Consumers: Data/Traces G3 and Infra G4; interpretation follows Cdx 3's [HAR-133 preregistration][har133]. Source inspection, not a training result. Library spend: **$0; no model, GPU, sandbox, or benchmark runs**.

## Decision summary for G3/G4

**Run one small, explicitly exploratory adapter experiment, not a claimed reproduction of a published scaling result.** The three coding papers below support success-filtered agent SFT, task-balanced selection, and separate evaluation. They do **not** establish a reliable gain from fewer than 50 trajectories with a 7–14B LoRA policy. There is one particularly relevant 36-trajectory **full-fine-tuning** result, and there are both modest self-improvement gains and a substantial regression.

Recommended fixed starting configuration, to record before training:

| Decision | Recommendation for this experiment | Basis |
|---|---|---|
| Collection | Keep G2's **30 training tasks × 2 attempts**; no adaptive resampling of promising tasks | Authorized plan; keeps the sampling denominator interpretable |
| Retention | At most **2 distinct, clean, counted-pass trajectories per task**, across all source batches combined; do not duplicate a single success to meet the cap | Plan and SWE-Gym's per-instance-cap ablation |
| Objective | Ordinary token cross-entropy on eligible **new assistant completion tokens**, with the exact captured conditioning context | Rejection-sampling SFT; not RL, preference training, or verifier training |
| LoRA | **rank 16, alpha 32, dropout 0.05**; freeze base weights; use only modules the intended vLLM deployment can actually load | Conservative engineering choice; matches the current tool's rank/alpha/dropout, not a paper-validated 9B recipe |
| Learning rate | **5e-5 peak** | Conservative recommendation for this tiny corpus; lower than the tool's 1e-4 default. Not copied from a LoRA policy experiment in the reviewed papers |
| Epochs | **2 fixed epochs**, no best-checkpoint selection against the 20 final tasks | Small, bounded pilot; fewer exposures than the 5-epoch SWE-Gym setting. Not evidence that two is optimal |
| Batch | Microbatch **1**, gradient accumulation **4** on one training GPU, effective batch **4 exported training units** | Recommendation to avoid the current accumulation-16 default leaving only a few optimizer updates when there are few units; record actual row, token, and update counts |
| Other training choices | Keep existing cosine scheduling and seed 42; record the realized warmup-step count, gradient checkpointing, optimizer, precision, and exact package pins. **No cross-example packing** tonight | Reuse the maintained trainer; prevent unrelated context from entering a target's prefix |
| Length | Accommodate the longest *faithful* training unit within the demonstrated memory/budget envelope, nominally up to the plan's ~60k tokens. **No silent truncation** | Current source defaults to 32,768 and right-truncates; that is not automatically compatible with G3 |
| Model choice | `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`; source pin `2367e865d009c13ac81713a2878291d33ab28177` at the inspected base | Fixed mission/model; verify Infra's actual served and trained revisions agree |

The hyperparameters are a **predeclared conservative recommendation**, not a search grid or permission to spend. Infra owns feasibility and the actual recorded config. A dry-run-driven change must be documented before final training and must not consult final-evaluation performance. Neither a low loss nor a working adapter is evidence of improved task performance.

### Scope and authority

The canonical plan is `~/Developer/research-context/inbox/sft-overnight-20260918/OVERNIGHT-2026-10-01.md`, written about 04:05Z, with the card map on [HAR-134][har134]. Peter's 03:55Z direction supersedes the older no-training text for **this one experiment**: stock distill versus LoRA-SFT distill on held-out FineEnvs MiMo-V2.6 Python tasks, with Terminus-2 `lf2`, Daytona task execution, and Modal inference. The shared overnight run cap is $30; G4 has $6 including its smoke check. Library has no run allocation. No RL, model/domain switch, completion-fix arm, new viewer, or new training framework is proposed here.

G1 freezes the evaluation set before training selection. G3 freezes the reviewed training manifest. G4 trains from that digest. G5 uses one LoRA-enabled server with two model names and otherwise identical harness, inference limits, and concurrency. Gate owners retain the canonical acceptance decisions. The later OOD benchmark qualification is **not** a substitute for tonight's frozen 20-task evaluation.

## 1. What method is being run?

**Rejection-sampling fine-tuning (RFT)** means generate candidate trajectories, apply a correctness/eligibility filter, and perform supervised learning on retained targets. Here RFT means *rejection-sampling fine-tuning*, not the sometimes-used abbreviation for *reinforcement fine-tuning*. Yuan et al. introduce the former terminology for correct mathematical reasoning paths; their results also favor diversity of paths over redundant copies [RFT, abstract and method][rft].

**Self-generated versus teacher-distilled data matters.** Tonight the already-distilled MiMo model generates the passes used to tune that same base model. This is success-filtered self-imitation. It is not a new distillation from a stronger teacher merely because the checkpoint name contains `Distill`. Historical HAR-104/110/116 trajectories may also come from different harness revisions; record their model revision, harness, limits, and capture policy instead of calling the whole mixture strictly on-policy under `lf2`.

**STaR is related but not identical.** STaR first trains on model-generated reasoning that yields correct answers. Its full procedure also supplies the correct answer to generate a rationale for previously failed examples, removes that hint for training, and repeats the generation/training loop. The paper resets to the original pretrained model for each round to reduce overfitting. Its few rationale demonstrations are prompts used to bootstrap a much larger problem pool, not proof that ten fine-tuning trajectories suffice [STaR §§3–4][star]. Tonight is one filtered-SFT update, with **no answer-conditioned rationalization, invented reasoning, or iterative self-training campaign**.

No original behavior-policy log probabilities are needed for ordinary SFT. That does not permit reconstructing unseen prompts or missing response text. The required evidence is the actual conditioning context and supervised tokens, plus eligibility and lineage.

## 2. What data counts?

A verifier reward of 1 is necessary under the plan but is not sufficient. Include a trajectory only when all of the following are evidenced:

1. Its task is in the training split, has ledger status `usable`, and is disjoint from final evaluation by repository/project identity and the registered near-duplicate check. Keep all segments and variants of one task together.
2. The immutable `counts` verdict is **`counted_pass`**, not merely a raw reward, an oracle/nop control, an incomplete attempt, or an uncountable copied-fix result.
3. Traces has marked the attempt clean and supplied a source-linked **cut step**. The cut excludes post-completion filler without removing the task-solving actions or making the surviving prefix a fabricated success.
4. Proxy requests and responses support the chosen targets. Preserve request order, task/harness/model identities, reasoning-capture policy, source digests, and the mapping from exported units back to assistant calls.
5. No verifier result, hidden tests, reference solution, post-run analysis, or synthetic success declaration enters model-visible training content. Reward and selection metadata stay outside the messages.
6. There are no more than two retained trajectories per task across the combined historical and G2 pool. Exact duplicate recordings and repeated prefix dumps do not become extra examples.

A trajectory that contains an ordinary failed test, a wrong hypothesis, and a later repair is not automatically bad data. Successful recovery can be useful. A passing trajectory can also contain undesirable actions; that is why the clean-review and cut-step requirements remain separate from the reward filter. Do not remove internal mistakes and splice the remaining steps together: later observations and actions would then have a context the actor never experienced.

If more than two eligible passes exist for one task, use a fixed rule on **training evidence only**: first require the same clean/fidelity conditions; then prefer lower post-cut assistant-token cost, with a stable source-ID tie-break. Record this bias toward shorter successful attempts. Do not rank samples by eventual performance of the trained adapter on the final set.

### Count independent data, not exported rows

Report at least: attempted tasks/trials, raw passes, counted passes, clean retained trajectories, unique tasks, unique repositories/projects, exported units, full input tokens, newly supervised tokens, and exclusions by reason. A single 100-call trajectory exported as 100 request/completion units is **one trajectory and one task**, not 100 independent demonstrations. The two-per-task cap applies to trajectories, not to every segment or assistant call.

The published SWE-Gym 491-trajectory corpus is a useful method example, not additional authorized data tonight. Its teachers are GPT-4o-2024-08-06 and Claude-3.5-Sonnet-20241022; no old teacher corpus is silently substituted for the selected MiMo self-generated data [SWE-Gym §4.2][swegym].

## 3. Exact context, loss masking, and summarisation

### 3.1 The fundamental unit is a captured prediction

For assistant call `j`, let `P_j` be the exact model input after the serving template and `C_j` the captured completion. The supervised example must represent `P_j + C_j`, with labels `-100` on `P_j` and padding, and loss only on eligible newly generated tokens in `C_j`. Tool observations, user/system text, copied historical assistant turns, and any assistant header/prefill already supplied by the request are **conditioning**, not new targets. Preserve genuine generated stop tokens where captured; do not invent an end-of-turn token for a response cut by a length limit.

TRL supports conversational prompt/completion data and pretokenized masks/labels; its pinned documentation distinguishes assistant-only from completion-only loss [TRL 1.14.0][trl]. The pinned implementation builds labels from `assistant_masks`, and applies a `completion_mask` when completion-only loss is enabled; existing `labels` are used as provided [TRL source][trl-source]. This establishes source support, **not** proof that our real batches receive the intended labels. Inspect the actual collated `labels` in G4 before training.

### 3.2 Why assistant-only loss is not sufficient

Consider this illustrative trace, not a real run:

```text
request 1: U0
response 1: reasoning R1 + action A1
request 2: U0, A1, observation O1       # R1 was not replayed
response 2: reasoning R2 + action A2
```

A flattened sample containing `U0, R1+A1, O1, R2+A2` teaches `A2` under a history containing `R1`, although request 2 did not contain it. Masking user tokens does not fix this. Conversely, removing every reasoning span and training empty thinking blocks plus actions is **not equivalent captured supervision**: each original action followed its own generated reasoning.

**Recommendation:** preserve per-request conditioning and supervise only that request's new completion. Coalesce several calls into one sequence only when each target's complete token prefix is proved equal to its actual serving input plus its own earlier output. This permits efficient multi-turn training where histories are genuinely prefix-consistent, without claiming that every ATIF transcript is such a history. Reuse the current export/trainer interfaces; the note does not authorize a replacement pipeline.

The MiMo template's assistant renderer places `reasoning_content` inside `<think>…</think>` for **every** assistant message, and emits an empty block when that field is absent [pinned template][mimo-template]. Historical reasoning must therefore follow the captured request, not be backfilled from a prior response. Supervise reasoning only when it is an actual captured emission with the correct prefix. If only parsed or partial response data survive, label the limitation and quarantine any target whose fidelity cannot be established; never synthesize a rationale to repair it.

Terminus-2's response format is part of this contract. Preserve the actual JSON/text emitted on this route. Do not convert it to a different tool-call language merely because a cited coding paper or the base model's native-tools documentation uses one.

### 3.3 Summarisation is a context boundary, not a second demonstration

After compaction, the actor sees a new summary/retained-history prefix. A target after that event must use that prefix, not the concatenated pre-compaction conversation. Distinguish:

- the actor's ordinary task-solving calls;
- a separate summarizer model's outputs;
- copied historical messages replayed into a continuation;
- new actor outputs after the handoff.

Only eligible actor outputs are new targets. A summarizer output that later becomes actor input is masked context; it is not automatically an actor completion. Copied assistant history remains masked when replayed. Deduplicate target spans by their source call identity, not just by identical text: the same short command can legitimately be issued at different points.

A failed summarisation attempt, synthetic harness fallback, mutated history without a complete recorded request, or missing continuation can destroy reconstructability. Exclude the affected unit with its source reason; do not infer a missing summary or reconstruct the history from the final state. Maintain the linkage between retained units, the reviewed trajectory, and its cut step.

### 3.4 Inspected implementation versus required G3 contract

At Eval Lab commit `a90a72913695686e05d52483453b2d4eb1e10cb0`:

| Source fact | Consequence for G3/G4 |
|---|---|
| [`sft_terminus.py`][exporter] emits one `{messages: …}` row per ATIF segment, with an `evallab.sft_terminus/1` manifest; default drops reasoning, `--keep-reasoning` adds it to assistant content | This older representation alone does not establish exact per-request fidelity for history-stripping inference |
| It rejects held-out contamination, parsed tool-call records, and unsplit summarisation; it excludes harness stand-ins and retains provenance | Reuse these safeguards, but do not equate raw reward selection with tonight's stricter `counted_pass` + clean-review gate |
| [`sft.py:render_and_mask`][trainer] compares incremental renders against its own reconstructed full conversation | Useful template consistency check; it does not compare those prefixes with the proxy's actual requests |
| Its template-derived mask covers all assistant turns in a row | Replayed/copied history can receive duplicate or inappropriate supervision unless the export/mask contract distinguishes new targets |
| Its default length is 32,768 and `keep_start` right truncation; fully masked rows are dropped | A run can silently lose the successful tail. Require zero unapproved truncations and explicit exclusion/feasibility accounting |
| GPU image pins include torch 2.14.0, Transformers 5.12.1, TRL 1.14.0, PEFT 0.21.0, Accelerate 1.15.0, datasets 5.0.1, huggingface_hub 1.9.2 | Bind any revised trainer receipt to the actual pins and module revisions; no assurance from latest documentation alone |

These are dated source findings, not claims that later owner fixes are absent. Data and Infra own the implementation disposition. Their final frozen contracts and smoke receipts must explicitly close these risks before the corresponding gate is accepted.

### 3.5 Minimum fidelity proof

Data's three-sample render check and Cdx 3's five-sample reconstruction audit should include both ordinary and post-summarisation requests where present, plus any historical-reasoning stripping case. Do not sample only the shortest clean-looking rows.

For those samples, prove:

1. Actual request payload/serving-template identity and exported prefix match; if original serving token IDs were not recorded, describe the weaker byte/template-equivalence proof rather than claiming recovered generation token IDs.
2. Every non-`-100` label belongs to the reviewed actor completion and no prompt/copied-history/observation/padding span contributes loss.
3. The pinned trainer's **collated batch** preserves those labels, including the causal label shift; a correct exporter mask alone is insufficient.
4. The cut step, summary boundary, response-end condition, and nontruncated final action are preserved.
5. Source digests and the export digest are stable; counts, token totals, and any exclusions reconcile.

Run lightweight structural checks over all rows as well as the manual audit. These are correctness checks on existing machinery, not permission to install a new modeling framework.

## 4. What the primary coding papers actually report

Sources are versioned: SWE-Gym v2 (2025-06-06), R2E-Gym v1 (2025-04-09), SWE-smith v2 (2025-05-21). Reported settings below are not interchangeable with tonight's fixed model, task format, or budget.

### Training settings: policy versus verifier, full fine-tuning versus LoRA

| Study / trained object | Data and model | Reported training settings | Applicability |
|---|---|---|---|
| SWE-Gym OpenHands **policy**, Appendix B.2 | 491 successful teacher trajectories; Qwen2.5-Coder-Instruct 7B/14B/32B | **Full fine-tuning**; lr `1e-4`, at most 5 epochs, global batch 8, context 32,768; 2–8 H100 80GB | Supports filtered trajectory SFT; not a LoRA recipe |
| SWE-Gym Moatless **policy**, §4.3 / Appendix B.3 | Self-generated successes; 7B | **Full fine-tuning**; lr `2e-5`, 5 epochs, batch 8, context 10,240 | Closest small-data self-imitation example, but different scaffold and parameter update |
| SWE-Gym Moatless **policy**, Appendix B.3 | Self-generated successes; 32B | **LoRA rank 64**, lr `5e-4`, 5 epochs, batch 8, context 10,240; one H100 | Outside requested 7–14B scale; alpha/dropout not specified here. Do not transfer the aggressive lr directly |
| R2E-Gym editing **policy**, §3 / Appendix B | 3,321 Sonnet trajectories from 2,048 tasks; Qwen2.5-Coder 7B/14B/32B | **Full SFT**; lr `1e-5`, 2 epochs, batch 8, warmup ratio 0.1, training context 20k | Larger teacher-distilled corpus, not tiny LoRA |
| R2E-Gym execution-free **verifier**, Appendix C.2 | 5,700 positive/negative trajectories; 14B | **LoRA rank 64**, lr `1e-5`, 2 epochs, batch 8, warmup 0.1, context 32k | Genuine reported 14B LoRA settings, but trains YES/NO judgments, **not the acting policy** |
| SWE-smith **policy**, §4 / Appendix F.1 | About 5k teacher trajectories for 32B; Table 3's 7B model uses 2k | **Full fine-tuning**; lr `5e-5`, at most 3 epochs, context 32,768; 2–8 H100 80GB | Source table does not report an agent LoRA rank; do not borrow a difficulty-rater script's defaults |

Primary links: [SWE-Gym][swegym], [R2E-Gym][r2egym], [SWE-smith][swesmith]. In these three papers, **no located experiment validates a 7–14B LoRA policy on fewer than 50 successful coding trajectories**. That is a scoped evidence gap, not a claim that such training cannot work. LoRA reduces the number of trainable parameters; its original paper does not establish this tiny-corpus agent setting either [LoRA][lora].

### What success filtering and balancing mean in these studies

- **SWE-Gym:** unit-test-successful trajectories; strong-teacher OpenHands distillation and a separate self-generated Moatless arm. Thirty rollouts per task in the self-improvement study yielded easy-task bias; capping retained trajectories at two per instance improved the balance. Tonight's **two collection attempts** are not a reproduction of those thirty attempts.
- **R2E-Gym:** successful Sonnet editing trajectories with thoughts and actions; train repositories exclude SWE-bench repositories. Collection allowed up to 32k tokens while SFT used 20k. That is the paper's scope, not permission for silent truncation in this experiment.
- **SWE-smith:** generated bugs must break passing tests; expert attempts are kept when resolved. At most three retained trajectories per task. Its synthetic tasks expose tests and are intended as training supply; they are not automatically a hidden-test evaluation replacement. The paper documents teacher/student tool-format conversion, underscoring that format matters rather than licensing arbitrary conversion here.

### Effect sizes, with the correct denominator and intervention

| Reported comparison | Result | What it does and does not establish |
|---|---|---|
| SWE-Gym 7B, OpenHands, 491 teacher trajectories, SWE-bench Verified | 1.8% → 10.6%, **+8.8 pp** | Full-FT teacher distillation on 500 evaluation tasks; not self-LoRA on <50 trajectories |
| SWE-Gym 14B, same setup | 4.0% → 16.4%, **+12.4 pp** | Same limitation |
| SWE-Gym 7B, Moatless cap ablation, SWE-bench Lite Table 6 | Base 7.0%; **36 trajectories / cap 1: 9.0%**; 62 / cap 2: 9.7%; 82 / cap 3: 7.7%; 172 / uncapped: 9.3% | The useful sub-50 precedent is full FT on a constrained scaffold; more passes are not monotonically better |
| SWE-Gym 7B, Moatless self-training iterations, Table 4 | 7.0% → 9.0% → 10.0% | Modest observed self-improvement, not a universal expected gain |
| SWE-Gym 32B, Moatless LoRA, same table | 19.0% → 19.7% → 19.7% | Small improvement then plateau |
| SWE-Gym 32B, OpenHands self-improvement attempt, §4.2 | 15.3% → **8.7%** on Lite after adding self-generated data | An important negative result: success-filtering alone is not sufficient |
| R2E-Gym 7B / 14B, Table 3 | Verified trained results **19.0% / 26.8%** after 3,321 teacher trajectories | Do not treat its different-harness comparisons with SWE-Gym as an isolated causal effect of data or of LoRA |
| SWE-smith 7B / 32B, Table 3 | Verified **15.2% / 40.2%** at 2k / about 5k training trajectories | Single-attempt results from large full-FT teacher corpora; not a forecast for tonight |

There is therefore **no defensible “typical improvement” number for this pilot**. Published gains range from modest positive changes to regressions, with larger teacher datasets often giving stronger results. Do not average these incomparable interventions or promise a particular pass-rate increase.

The coding papers evaluate on SWE-bench Lite (300 tasks) and/or Verified (500), with training/evaluation repository separation in their main setups. Their policy pass@1 results must be kept separate from verifier-driven **Best@k**, which selects among multiple attempts, and oracle **Pass@k**, which asks whether any attempt passed. R2E-Gym's 51% headline uses hybrid verification with 26 editing attempts; it is not its single-attempt policy score. Tonight is one attempt per arm per task, not test-time search.

## 5. Overfitting and budget at fewer than 50 trajectories

### Expected risks, not observed overnight findings

- **Easy-task selection:** successes overrepresent tasks the base already solves. The two-per-task cap helps but does not create coverage of unsolved behaviors.
- **Repository/format memorization:** many turns from one repository are not broad evidence. Keep repository/project-level disjointness and report concentration.
- **Trajectory length weighting:** token-mean cross-entropy gives longer responses more weight. Splitting into more rows can also change update frequency. Report per-task supervised tokens and the loss reduction/batching policy; do not equate equal task caps with equal token influence.
- **Repeated-prefix supervision:** copied history can inflate both sample count and gradient contribution. Masking copied context is more important than adding epochs.
- **Selective checkpoint reporting:** choosing epochs, masks, or rank by the final 20 tasks turns that set into development data. Use the fixed final checkpoint; a preexisting dry-run adapter must not become the trained arm.
- **False confidence from loss:** falling train loss measures imitation of retained targets. It does not measure repair success, generalization, or absence of regressions.

The small sample size does not justify inventing an arbitrary minimum-N success gate, adding external training data, or launching an unapproved sweep. Report the actual number of clean independent tasks available. A very small corpus may support only a mechanism/behavioral pilot even when every engineering gate passes.

### Practical G4 boundary

The current tool's 1,000-token/s training assumption and approximately $2.814912/hour A100 container rate are **estimation inputs**, not measured throughput for these long sequences [trainer][trainer]. Gradient checkpointing is not proof that 60k-token samples fit, nor that they fit within $6. In a per-request export, repeated context consumes compute even when its labels are masked.

Before the final job, Infra should use its authorized dry run to measure peak memory, real step time, and startup/tail cost on representative long units. Budget from the **sum of all rendered input tokens across both epochs**, the actual sequence-length distribution, and observed throughput—not from supervised tokens alone or “under 50 examples.” Leave room for serving/parity smoke inside the G4 allocation. If a faithful sample is too long, stop and document an exclusion or an owner-approved configuration change before freezing/training; do not discard its completion silently.

Use the exact module subset validated by the intended **vLLM** route. The inspected SFT tool's q/k/v/o/gate/up/down target list was justified against **SGLang** and omits hybrid linear-attention modules. That is not vLLM compatibility proof. Do not broaden to `all-linear` without proving that the adapter both trains and reloads on the serving path. This is a G4 compatibility check, not a model change.

## 6. Interpreting the frozen 20-task paired evaluation

[HAR-133][har133] owns the preregistration: counted pass rate paired by task; exact two-sided McNemar at alpha 0.05; fixed infra-failure/exclusion rules; secondary tokens/loops/stop reasons. This note explains the sensitivity, not a competing analysis plan.

With 20 complete pairs, **one task is five percentage points**. Record the whole paired table: both pass, stock-only pass, tuned-only pass, both fail. A non-significant result is not evidence of equivalence or of “no effect.” One run per task also leaves stochastic run-to-run variance largely unmeasured.

For `w` tuned-only wins and `l` stock-only wins, the two-sided exact calculation on `d = w + l` discordant pairs is `min(1, 2 * sum(comb(d, k) for k in range(min(w, l) + 1)) / 2**d)`; use 1 when `d=0` [McNemar reference][mcnemar]. Examples below were calculated with Python's standard-library `math.comb`; **they are illustrations, not experimental results**:

| Tuned-only / stock-only | Net change out of 20 | Exact two-sided p |
|---|---:|---:|
| 4 / 0 | +20 pp | 0.125 |
| 5 / 0 | +25 pp | 0.0625 |
| 6 / 0 | +30 pp | 0.03125 |
| 7 / 1 | +30 pp | 0.0703125 |
| 8 / 1 | +35 pp | 0.0390625 |

Thus even a five-task improvement with no regressions does not cross the specified threshold. For scale, a marginal 10/20 pass rate has a Wilson 95% interval of approximately **29.9%–70.1%** under independent-binomial assumptions; that is not a confidence interval for the paired difference. Shared-repository tasks can be dependent, and a light-image convenience slice is not a random sample of all coding work. Report the corpus and clustered structure alongside the preregistered statistic instead of generalizing to all Python tasks.

Keep infrastructure/capture failures separate from measured model failures. Apply the registered counts/exclusion/rerun policy, preserve the original evidence, and report both scheduled and analyzable denominators. Do not replace difficult tasks, retry only one arm's ordinary failures, or optimize against this evaluation after seeing the result. G6's arm-blind trace analysis should explain concrete changes in loops, JSON format, completion behavior, and time to first edit without promoting secondary findings into a post-hoc primary win.

## 7. Gate receipt checklist

**G3 / Data + Traces:** frozen eval digest precedes selection; train/eval disjointness evidence; every source is a clean counted pass with cut step; at most two trajectories/task; proxy-call/summary provenance; exact-target mask policy; distinct-task and token census; all exclusions; export/data-card digest; reconstruction checks include difficult boundary cases.

**G4 / Infra:** base/tokenizer/template revisions; actual package pins and adapter targets; declared hyperparameters and realized optimizer-step count; zero unapproved truncations; real collated-mask proof; startup/training/serving cost; finite loss/gradient observations; adapter-file digest and successful reload; both model names; five Terminus-2 JSON smoke prompts per arm and base-serving parity evidence; auto-stop. A serving smoke establishes operability, not capability.

**G5/G6 / Engineering + Traces + Cdx 3:** same frozen tasks, `lf2` digest, limits/concurrency and shared server; alternating arm order; counts/provenance for every attempt; preregistered paired table/statistic and exclusions; arm-blind behavior analysis; explicit valid/valid-with-caveats/invalid verdict. No external benchmark is substituted without a later decision.

## Primary sources and inspected artifacts

- **SWE-Gym:** Pan et al., *Training Software Engineering Agents and Verifiers with SWE-Gym*, [arXiv:2412.21139v2][swegym], especially §§4.1–4.3, Tables 3/4/6, Appendices B.2–B.4. [Released repository](https://github.com/SWE-Gym/SWE-Gym).
- **R2E-Gym:** Jain et al., *Procedural Environments and Hybrid Verifiers for Scaling Open-Weights SWE Agents*, [arXiv:2504.07164v1][r2egym], §3, Tables 3–4, Appendices B/C.2. [Released repository](https://github.com/R2E-Gym/R2E-Gym).
- **SWE-smith:** Yang et al., *Scaling Data for Software Engineering Agents*, [arXiv:2504.21798v2][swesmith], §§3–4, Table 3, Appendices A.1/F.1/F.3. Main text reports 5,016 trajectories; the released snapshot and curated-pool descriptions have slightly different counts. Use the scoped count, not a silently reconciled total. [Released repository](https://github.com/SWE-bench/SWE-smith).
- **RFT:** Yuan et al., [arXiv:2308.01825v2][rft]. Mathematical reasoning, not a coding-agent or tiny-LoRA validation.
- **STaR:** Zelikman et al., [arXiv:2203.14465v2][star], §§3–4. GPT-J 6B reasoning experiments, not tonight's model or harness.
- **LoRA:** Hu et al., [arXiv:2106.09685v2][lora]. Mechanism reference, not a prescription for this corpus.
- **Trainer:** [TRL v1.14.0 SFT documentation][trl] and [pinned SFTTrainer implementation][trl-source].
- **Model formatting:** [MiMo pinned chat template][mimo-template]; base model revision is taken from the inspected Lab tool.
- **Local implementation baseline:** [exporter][exporter], [training tool][trainer], [training tool limitations][trainer-readme] at `a90a72913695686e05d52483453b2d4eb1e10cb0`. Later owner receipts supersede only the implementation-status observations, not the fidelity requirements.

[har134]: https://linear.app/petermakhnatch/issue/HAR-134
[har133]: https://linear.app/petermakhnatch/issue/HAR-133
[swegym]: https://arxiv.org/pdf/2412.21139v2
[r2egym]: https://arxiv.org/pdf/2504.07164v1
[swesmith]: https://arxiv.org/pdf/2504.21798v2
[rft]: https://arxiv.org/abs/2308.01825v2
[star]: https://arxiv.org/html/2203.14465v2
[lora]: https://arxiv.org/abs/2106.09685v2
[trl]: https://huggingface.co/docs/trl/v1.14.0/en/sft_trainer
[trl-source]: https://github.com/huggingface/trl/blob/v1.14.0/trl/trainer/sft_trainer.py#L1615-L1640
[mimo-template]: https://huggingface.co/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B/blob/2367e865d009c13ac81713a2878291d33ab28177/chat_template.jinja
[exporter]: https://github.com/PeterMakhnatch/eval-lab/blob/a90a72913695686e05d52483453b2d4eb1e10cb0/src/evallab/sft_terminus.py
[trainer]: https://github.com/PeterMakhnatch/eval-lab/blob/a90a72913695686e05d52483453b2d4eb1e10cb0/tools/modal-mimo-sft/sft.py
[trainer-readme]: https://github.com/PeterMakhnatch/eval-lab/blob/a90a72913695686e05d52483453b2d4eb1e10cb0/tools/modal-mimo-sft/README.md
[mcnemar]: https://www.statsmodels.org/stable/generated/statsmodels.stats.contingency_tables.mcnemar.html
