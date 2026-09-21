# GLM-5.3-Flash SFT Experiment & Serving Specification

## 1. Overview
* **Target Model:** `zai-org/GLM-5.3-Flash` (Selector: `zai/glm-5.3-flash`)
* **Architecture:** `Glm5NextForConditionalGeneration` (hybrid 34 linear attention + 11 full sparse attention layers)
* **Parameters:** 320B total parameters, 18B active parameters per token (288 routed experts + 1 shared expert, top-8 routing).
* **Context Length:** 1,048,576 tokens (1M context window).
* **Pinned revision (model + tokenizer):** `eb9eb208eb0d988989d07a6a12d0fdeb5f52574a` (HAR-66 qualification packet: `research/experiments/glm53-sft-qualification/`). Every `from_pretrained` in the generated trainer passes it as `revision=`; `sft_config.json` pins it in `model_revision` / `tokenizer_revision`.

## 2. Hardware Envelope & Memory Arithmetic (`"qualification": "estimate-unverified"`)
All numbers below are storage arithmetic with **no target-model run receipt**; treat GPU counts and topologies as unverified estimates, not qualified allocations.
* **Weight VRAM:**
  * FP8: 320 GB
  * BF16: 640 GB
* **Full SFT:** ~72$\times$ H100 80GB SXM (9 nodes $\times$ 8, Megatron-Core / FSDP) estimated from ~2.5–5.1 TB AdamW optimizer state footprint.
* **LoRA (Recommended):** Base weights frozen in FP8 (320 GB), ~320M adapter parameters ($<5$ GB optimizer VRAM). Estimated to fit **1 node of 8x H100 80GB SXM** (640 GB aggregate VRAM).
* **QLoRA:** 4-bit base (~160 GB); hardware sized at `int4`, estimated 1 node of 4x H100 (Slurm derives `--nodes=ceil(gpus/8)` per method).
* **Serving Node:** 8x H100 80GB SXM (Tensor Parallelism = 8) or 4x H200 141GB SXM with vLLM / SGLang.
* **Task Sandbox:** Isolated execution container (CPU/Docker).
* **Operator Machine:** Local preparation of manifests, tokenization validation, and remote job submission.

## 3. Loss Masking Contract
* **Prefix:** `[gMASK]<sop>` (unsupervised, `mask=0`, `label=-100`).
* **System Prompt:** `<|system|>...` (unsupervised, `mask=0`, `label=-100`).
* **User Turns:** `<|user|>...` (unsupervised, `mask=0`, `label=-100`).
* **Assistant Header:** `<|assistant|>` delimiter is NOT supervised.
* **Assistant Content:** Supervised (`mask=1`, `label=token_id`), including `<think>`...`</think>` reasoning blocks and `<tool_call>`...`</tool_call>` actions.
* **Tool Responses:** `<|observation|><tool_response>...</tool_response>` (unsupervised, `mask=0`, `label=-100`).
* **Padding:** `<|endoftext|>` (id 154820) (unsupervised, `mask=0`, `label=-100`).
* The generated trainer sets `SFTConfig(assistant_only_loss=True)`, which is what makes the patched template's `{% generation %}` markers supervise assistant tokens only. Known limitation (HAR-66 #9): the template appends no end-of-turn token after the final assistant turn, so TRL warns the model "may not learn to stop"; define an explicit target-boundary policy before real training.

## 4. Serving & RE Handoff
* **Serving Command:**
  ```bash
  vllm serve zai-org/GLM-5.3-Flash \
      --enable-lora \
      --lora-modules glm-flash-sft-v1=runs/sft-glm-flash-checkpoints/final \
      --max-model-len 32768 \
      --tensor-parallel-size 8 \
      --chat-template research/experiments/sft-glm-flash/chat_template.jinja
  ```
* **Merge before serving:** dynamic LoRA is NOT supported for `glm5_next` in vLLM/SGLang executors (HAR-66); merge the adapter into the base weights before serving.
* **Checkpoint manifest contract:** the trainer writes `runs/sft-glm-flash-checkpoints/final/checkpoint_manifest.json` via `evallab.sft_glm.write_checkpoint_manifest` (base model id + revision, tokenizer revision, library versions, dataset path + sha256, config sha256, `train_result` metrics, mask receipt summary, timestamp); validate with `evallab.sft_glm.verify_checkpoint`. Full handoff: `re_serving_handoff.json`.
* **Evaluation Seam:** Fixed mini-SWE-agent on TB4 test splits, evaluating identical harness parameters before and after LoRA weight application.

## 5. Training-Record Field Mapping (HAR-65 → trainer)
The trainer consumes HAR-65 record JSONL (`src/evallab/sft_records.py`, PR #446); the adapter is `evallab.sft_glm.har65_record_to_messages` (fail closed, never silent repair):
* `decision_example` rows → `messages = context + [target]` (context is strictly pre-target; the target's own observations are absent by construction).
* `full_trajectory` rows (and legacy rows with a bare `messages` list) → `messages` as-is.
* Per message only the template-read keys survive: `role`, `content` (`None` → `""`), plus `reasoning_content` and `tool_calls` on assistant turns. `outcome` (reward/verifier), `lineage`, `split`, `fidelity`, `step_id`, `presented_as` are never read and cannot leak into the learner.
* Assistant `tool_calls[].arguments` stays a mapping: JSON-string arguments are decoded (HAR-66 #6; the template rejects raw strings), Python literals likewise; anything else raises.
* Fail closed (`ValueError`): redacted messages, reward/verifier metadata keys (`verified_success`, `reward`, `grade`, `tests_passed`, …) inside any message, unknown roles, non-string content, records with no assistant turn. `GLMTrainingRecord.validate_causal_ordering` additionally rejects `tool`/`observation` messages with no preceding `assistant` message and reward keys in record-level `metadata`.

## 6. Isolated Pinned Environment
Heavy training deps stay OUT of the core `pyproject.toml`/`uv.lock` by mission rule. The isolated recipe is `requirements-sft.txt`:
```bash
python -m venv /tmp/sft-glm
/tmp/sft-glm/bin/python -m pip install -r research/experiments/sft-glm-flash/requirements-sft.txt
```
* Pins: `torch==2.14.0`, `trl==1.13.0`, `peft==0.21.0`, `accelerate==1.15.0`, `datasets==5.0.1` — all verified by the plumbing canary below in `/tmp/test-torch`.
* `transformers` uses HAR-66's pin (`git ... @770e4c40…`, required for `glm5_next` model support) **instead of** the canary-verified `5.17.0` wheel: HAR-66's pins win where they overlap, and the git pin has NOT been re-verified in this venv — re-verify on GPU before the first real training run. `transformers==5.17.0` registers the `glm5_next` config and `AutoModelForImageTextToText` names (checked live), which is why the canary constructs, but exact-model weight loading is unproven here.
* `bitsandbytes` (qlora 4-bit load only) has no verified macOS pin in this venv; pin it at GPU qualification — qlora must not run without it.
* No model weights are downloaded: the GLM tokenizer is used from the HF cache at the pinned revision with `HF_HUB_OFFLINE=1`.

## 7. Plumbing Canary (NOT GLM-5.3-Flash)
A tiny random-init Llama (`hidden_size=32`, 2 layers, vocab sized to the real GLM tokenizer: `len(tokenizer) == 154856`, i.e. 154820 base ids + 36 added special tokens) plus the **real** GLM tokenizer from the pinned cache snapshot proves the generated script constructs (`SFTConfig`/`SFTTrainer` under trl 1.13.0), trains, writes `checkpoint_manifest.json`, reloads, and passes `verify_checkpoint`. It exercises plumbing only and says nothing about GLM capability. Canonical command (builds model + 4-record dataset, runs `max_steps=2` on CPU):
```bash
PYTHONPATH=src HF_HUB_OFFLINE=1 /tmp/test-torch/bin/python -m pytest tests/test_sft_glm.py -q -p no:xdist -k "generated_trainer or patched_upstream"
```
Receipt (manual equivalent, `train.py` generated from the committed default config with model/dataset/output redirected to the stand-in, `max_steps=2`; exit 0 — literal last 10 log lines):
```
Dropping fully masked examples from train dataset:   0%|          | 0/4 [00:00<?, ? examples/s]Dropping fully masked examples from train dataset: 100%|██████████| 4/4 [00:00<00:00, 6676.17 examples/s]
=== Launching SFT Training ===
[transformers] The tokenizer has new PAD/BOS/EOS tokens that differ from the model config and generation config. The model config and generation config were aligned accordingly, being updated with the tokenizer's values. Updated tokens: {'eos_token_id': 154820, 'bos_token_id': None}.
  0%|          | 0/2 [00:00<?, ?it/s]/tmp/test-torch/lib/python3.12/site-packages/torch/utils/data/dataloader.py:759: UserWarning: 'pin_memory' argument is set as true but not supported on MPS now, device pinned memory won't be used.
  super().__init__(loader)
 50%|█████     | 1/2 [00:00<00:00,  5.20it/s]                                             {'train_runtime': '0.3322', 'train_samples_per_second': '48.16', 'train_steps_per_second': '6.02', 'train_loss': '11.96', 'entropy': '11.94', 'num_tokens': '504', 'mean_token_accuracy': '0', 'epoch': '2'}
100%|██████████| 2/2 [00:00<00:00,  5.20it/s]100%|██████████| 2/2 [00:00<00:00,  6.03it/s]
Training finished. Manifest: /tmp/canary/out/final/checkpoint_manifest.json
Loading weights:   0%|          | 0/21 [00:00<?, ?it/s]Loading weights: 100%|██████████| 21/21 [00:00<00:00, 81707.22it/s]
Save/reload receipt: forward pass matches
```
The test additionally asserts the manifest's `supervised_tokens` equals an independent `GLMLossMasker` count on the same first record (30 == 30 in this run).
