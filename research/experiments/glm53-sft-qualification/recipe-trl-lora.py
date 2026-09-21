"""HAR-66 consumer skeleton for Helper (HAR-64): TRL SFT + PEFT LoRA on GLM-5.3-Flash.

NOT a runnable trainer yet: dataset plumbing, node launcher, and the
prompt-completion record loader come from Harness' export + Helper's mission.
Every constraint below is source-verified; see ../next-task.json and
../verification.json. No GPU execution happened in HAR-66.

Hard requirements encoded here:
  - transformers pinned to git commit (PyPI 5.16.0 lacks glm5_next).
  - AutoModelForImageTextToText (not CausalLM); explicit object into SFTTrainer.
  - BF16 dequantized base; SDPA attention (FlashAttention unsupported).
  - LoRA on 2D nn.Linear targets only; routed experts are 3D nn.Parameter.
  - Prompt-completion records; assistant_only_loss is unusable on stock template.
"""

from __future__ import annotations

import torch
from peft import LoraConfig
from transformers import AutoModelForImageTextToText, AutoTokenizer
from trl import SFTConfig, SFTTrainer

MODEL_ID = "zai-org/GLM-5.3-Flash"
# Pin the revision the probe qualified. Resolve once, record in the receipt.
MODEL_REVISION = "eb9eb208eb0d988989d07a6a12d0fdeb5f52574a"

# KDA (34 layers) + DSA (11 layers) + dense MLP (0-2) + shared experts (3-44).
# Routed experts (288/layer, 3D nn.Parameter) are intentionally absent.
LORA_TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj", "b_proj", "g_a_proj", "g_b_proj",
    "q_a_proj", "q_b_proj", "kv_a_proj_with_mqa", "kv_b_proj",
    "gate_proj", "up_proj", "down_proj",
]


def build_trainer(train_dataset) -> SFTTrainer:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token  # <|endoftext|> id 154820

    model = AutoModelForImageTextToText.from_pretrained(  # NOT CausalLM
        MODEL_ID,
        revision=MODEL_REVISION,
        torch_dtype=torch.bfloat16,      # dequantizes the FP8 checkpoint
        attn_implementation="sdpa",      # _supports_flash_attn = False
    )

    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        target_modules=LORA_TARGET_MODULES,
        task_type="CAUSAL_LM",
    )

    args = SFTConfig(
        output_dir="./output/glm53-flash-lora",
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=1e-4,
        lr_scheduler_type="cosine",
        warmup_steps=0.03,  # TRL 1.13+: fractional steps express the warmup ratio.
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        logging_steps=10,
        save_strategy="steps",
        save_steps=100,
        # Stock template has no {% generation %} tags: assistant_only_loss
        # supervises zero tokens. Records must be {"prompt": ..., "completion": ...}.
        completion_only_loss=True,
        max_length=4096,
        packing=False,
    )

    return SFTTrainer(
        model=model,                      # explicit object, never the bare string
        args=args,
        train_dataset=train_dataset,
        processing_class=tokenizer,       # prevents VLM-collator default
        peft_config=peft_config,
    )


def merge_adapter(adapter_dir: str, output_dir: str) -> None:
    """Merge before serving: vLLM/SGLang have no dynamic LoRA for glm5_next."""
    from peft import PeftModel

    base = AutoModelForImageTextToText.from_pretrained(
        MODEL_ID, revision=MODEL_REVISION, torch_dtype=torch.bfloat16
    )
    merged = PeftModel.from_pretrained(base, adapter_dir).merge_and_unload()
    merged.save_pretrained(output_dir, safe_serialization=True)
    # Merged BF16 ≈ 598 GiB: plan an H200-class serving node, or a separate
    # FP8 re-quantization step before an 8xH100 serve.
