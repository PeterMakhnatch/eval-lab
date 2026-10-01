#!/usr/bin/env python3
"""Local proof of sft.py's training path on the pinned TRL stack (HAR-129, $0).

Runs on the local accelerator (Apple MPS here, else CPU), in fp32; the GPU
path differs in bf16, CUDA kernels, Qwen3_5/fla and the real weights.

Uses the real distill tokenizer and the $0 fixture (including a G3-shaped
``loss: "last"`` row), a tiny randomly initialised Qwen2 model with the
tokenizer's vocabulary, and the exact ``build_trainer`` /
``collated_label_check`` / ``lora_digest`` that ``train_remote`` runs, with
the G4 hyperparameters (dropout 0.05, lr 5e-5 cosine, 3% warmup, seed 42)
except accumulation 2, so the 4 fixture rows make 2 optimizer steps. It proves:

1. TRL's prepared rows match the rendered rows (count, order, input_ids) and
   the collated labels equal the rendered masks;
2. the chunked selected-token loss equals an independent full-logits
   cross-entropy over the same labels;
3. the seed is applied before PEFT init: two ambient RNG states (101, 202)
   give the same initial LoRA_A digest;
4. a real update happens: a positive learning rate is logged and LoRA_B,
   zero at init, is nonzero after training.

    uv run --project tools/modal-mimo-sft --locked \\
      --with torch==2.14.0 --with trl==1.14.0 --with peft==0.21.0 \\
      --with accelerate==1.15.0 --with datasets==5.0.1 \\
      python research/experiments/har129-lora/cpu_trl_proof.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/modal-mimo-sft"))

import sft  # noqa: E402
import torch  # noqa: E402
import transformers  # noqa: E402
import trl  # noqa: E402
from transformers import Qwen2Config, Qwen2ForCausalLM  # noqa: E402


def main() -> int:
    tokenizer = sft.get_tokenizer()
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    export = sft.verify_export(ROOT / "tools/modal-mimo-sft/fixtures/tiny-export")
    rows = []
    for index, row in enumerate(export.conversations):
        rendered = sft.render_and_mask(
            tokenizer,
            row.messages,
            max_length=sft.DEFAULT_MAX_LENGTH,
            label=f"conversation {index}",
            loss=row.loss,
        )
        rows.append({"input_ids": rendered.input_ids, "assistant_masks": rendered.mask})

    def tiny_model() -> Qwen2ForCausalLM:
        torch.manual_seed(0)
        model = Qwen2ForCausalLM(
            Qwen2Config(
                vocab_size=len(tokenizer),
                hidden_size=32,
                intermediate_size=64,
                num_hidden_layers=2,
                num_attention_heads=4,
                num_key_value_heads=2,
                max_position_embeddings=1024,
                tie_word_embeddings=False,
            )
        )
        model.config.use_cache = False
        return model

    config = {
        **sft.DEFAULTS,
        "target_modules": list(sft.DEFAULT_LORA_TARGET_MODULES),
        "grad_accum_steps": 2,
    }
    assert sft.optimizer_steps(len(rows), config) == 2

    # Seed before PEFT init: same configured seed, two ambient RNG states.
    lora_a = {}
    for ambient in (101, 202):
        with tempfile.TemporaryDirectory() as out:
            model = tiny_model()
            torch.manual_seed(ambient)
            seeded = sft.build_trainer(
                model, tokenizer, rows, config, output_dir=Path(out), bf16=False
            )
            lora_a[ambient] = sft.lora_digest(seeded.model, "A")["sha256"]

    model = tiny_model()
    with tempfile.TemporaryDirectory() as out:
        trainer = sft.build_trainer(
            model, tokenizer, rows, config, output_dir=Path(out), bf16=False
        )
        lora_b_init = sft.lora_digest(trainer.model, "B")["max_abs"]
        trainer.model.eval()
        check = sft.collated_label_check(trainer, rows)

        # Chunked loss vs an independent full-logits cross-entropy over the
        # same shifted labels (not the model's own, possibly TRL-patched, loss).
        # A 7-token chunk forces several chunks per row.
        sft.LOSS_CHUNK_TOKENS = 7
        diffs = []
        for example in trainer.train_dataset:
            batch = trainer.data_collator([example])
            batch = {key: value.to(trainer.model.device) for key, value in batch.items()}
            ours = trainer.compute_loss(trainer.model, batch).item()
            logits = trainer.model(input_ids=batch["input_ids"]).logits[0, :-1].float()
            reference = torch.nn.functional.cross_entropy(
                logits, batch["labels"][0, 1:], ignore_index=-100
            ).item()
            diffs.append(abs(ours - reference))
        sft.LOSS_CHUNK_TOKENS = 4096
        trainer.model.train()

        # A broken mask must be caught.
        broken = [dict(rows[0], assistant_masks=[0] + rows[0]["assistant_masks"][:-1])]
        try:
            sft.collated_label_check(trainer, broken + rows[1:])
            caught = False
        except RuntimeError:
            caught = True

        result = trainer.train()
        lora_b_final = sft.lora_digest(trainer.model, "B")["max_abs"]
        learning_rates = [
            entry["learning_rate"]
            for entry in trainer.state.log_history
            if "learning_rate" in entry
        ]
    print(
        json.dumps(
            {
                "versions": {
                    "torch": torch.__version__,
                    "trl": trl.__version__,
                    "transformers": transformers.__version__,
                },
                "collated_label_check": check,
                "loss_rows": [row.loss for row in export.conversations],
                "max_abs_loss_diff_vs_stock": max(diffs),
                "shifted_mask_caught": caught,
                "train_steps": result.global_step,
                "train_loss": result.training_loss,
                "lora_A_init_sha256_by_ambient_seed": lora_a,
                "learning_rates": learning_rates,
                "lora_B_max_abs_init_final": [lora_b_init, lora_b_final],
            },
            indent=1,
        )
    )
    assert max(diffs) < 1e-4 and caught and check["mismatched_rows"] == 0
    assert lora_a[101] == lora_a[202]
    assert lora_b_init == 0.0 and lora_b_final > 0.0 and max(learning_rates) > 0.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
