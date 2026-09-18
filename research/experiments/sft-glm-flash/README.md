# GLM-5.3-Flash SFT Experiment & Serving Specification

## 1. Overview
* **Target Model:** `zai-org/GLM-5.3-Flash` (Selector: `zai/glm-5.3-flash`)
* **Architecture:** `Glm5NextForConditionalGeneration` (hybrid 34 linear attention + 11 full sparse attention layers)
* **Parameters:** 320B total parameters, 18B active parameters per token (288 routed experts + 1 shared expert, top-8 routing).
* **Context Length:** 1,048,576 tokens (1M context window).

## 2. Hardware Envelope & Memory Arithmetic
* **Weight VRAM:**
  * FP8: 320 GB
  * BF16: 640 GB
* **Full SFT:** Requires $\ge 72\times$ H100 80GB SXM nodes (Megatron-Core / FSDP) due to ~2.5–5.1 TB AdamW optimizer state footprint.
* **LoRA (Recommended):** Keeps base weights frozen in FP8 (320 GB) and trains ~320M adapter parameters ($<5$ GB optimizer VRAM). Fits comfortably on **1 node of 8x H100 80GB SXM** (640 GB aggregate VRAM).
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
* **Evaluation Seam:** Fixed mini-SWE-agent on TB4 test splits, evaluating identical harness parameters before and after LoRA weight application.
