"""TRL LoRA SFT of XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B on Modal (HAR-81).

Trains a PEFT LoRA adapter on an ``evallab.sft_terminus`` export (Terminus-2
teacher trajectories as ``{"messages": [{"role", "content"}]}`` rows) with the
distill's OWN chat template, assistant-only loss, and thinking mode matching
the served route (``chat_template_kwargs.enable_thinking=True``).

Subcommands (from the repository root)::

    # $0 offline check: render every conversation, verify assistant-only
    # masks, print token stats and the cost estimate. Needs only the
    # tokenizer files (downloaded once from the public HF repo, then cached).
    uv run --project tools/modal-mimo-sft --locked \\
        python tools/modal-mimo-sft/sft.py dry-run \\
        --data tools/modal-mimo-sft/fixtures/tiny-export
    # Gated GPU steps: print the estimate first, refuse without --confirm-spend.
    uv run --project tools/modal-mimo-sft --locked \\
        python tools/modal-mimo-sft/sft.py train --data <export> --confirm-spend
    uv run --project tools/modal-mimo-sft --locked \\
        python tools/modal-mimo-sft/sft.py merge --adapter <run>/adapter --confirm-spend

Key design points (see README.md for evidence and the cost formula):

- Rendering uses ``tokenizer.apply_chat_template`` with the model's own
  ``chat_template.jinja``. A leading ``<think>...</think>`` content prefix
  (the ``kept_as_think_prefix`` reasoning policy) maps to the template's
  ``reasoning_content`` field; anything else stays verbatim in ``content``.
- Assistant-only loss comes from the template's ``{% generation %}`` markers
  (``return_assistant_tokens_mask``) cross-checked against incremental prefix
  rendering with a verified prefix property. Any disagreement, any prefix
  break, or any conversation with zero trainable tokens raises loudly.
- The Modal training image pins exact GPU deps (torch, transformers, trl,
  peft, accelerate, datasets); the local lock stays CPU-only (no torch).
- SGLang v0.5.20 natively serves PEFT LoRA adapters for the qwen3_5 hybrid
  (see README.md), so the default path is adapter + ``--lora-paths``; ``merge``
  exists as the gated fallback that writes merged bf16 weights.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import modal

MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
# Hugging Face commit of the release weights (2026-09-22); same pin as serve.
MODEL_REVISION = "2367e865d009c13ac81713a2878291d33ab28177"
APP_NAME = "evallab-mimo-v26-9b-sft"

EXPORT_CONTRACT = "evallab.sft_terminus/1"
CONVERSATIONS_FILE = "conversations.jsonl"
MANIFEST_FILE = "manifest.json"
ALLOWED_ROLES = frozenset({"system", "user", "assistant"})

WEIGHTS_VOLUME = "evallab-mimo-v26-9b-weights"
SFT_VOLUME = "evallab-mimo-v26-9b-sft"
WEIGHTS_MOUNT = "/weights"
SFT_MOUNT = "/sft"
BASE_DIR = f"{WEIGHTS_MOUNT}/{MODEL_ID}/{MODEL_REVISION}"

# Modal A100-80GB time rate, identical to the HAR-90 serve container shape
# (1x A100-80GB, 4 CPU, 16 GiB): $2.4984 + $0.18864 + $0.127872 per hour.
# Source: modal.com/pricing via src/evallab/execution_contracts.py
# MIMO_SELFHOSTED_SERVER_USD_PER_HOUR (2026-09-28).
MODAL_USD_PER_HOUR = 2.814912
# Labeled throughput assumption for the time estimate: 9B bf16 LoRA,
# grad checkpointing, ~32K context on one A100-80GB. Rough on purpose;
# the dry run prints it next to every estimate and the receipt records it.
ASSUMED_TRAIN_THROUGHPUT_TOK_S = 1000.0

DEFAULT_MAX_LENGTH = 32768
# Right-truncate (TRL SFTConfig truncation_mode="keep_start"); conversations
# left with zero trained tokens after truncation are dropped, mirroring
# SFTTrainer preparation. Recorded in every receipt.
TRUNCATION_POLICY = "keep_start:right-truncate@max_length,drop-fully-masked"

# PEFT LoRA targets restricted to modules SGLang v0.5.20 serves for qwen3_5:
# SUPPORTED_LORA_TARGET_MODULES (python/sglang/srt/utils/common.py) mapped
# through Qwen3_5ForCausalLM.packed_modules_mapping onto qkv_proj/o_proj/
# gate_up_proj/down_proj. Linear-attention in_proj_qkvz/out_proj are left
# out of the default (see README.md).
DEFAULT_LORA_TARGET_MODULES = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
]
DEFAULTS = {
    "lora_rank": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05,
    "learning_rate": 1e-4,
    "lr_scheduler": "cosine",
    "warmup_ratio": 0.03,
    "num_epochs": 1,
    "per_device_batch": 1,
    "grad_accum_steps": 16,
    "max_length": DEFAULT_MAX_LENGTH,
    "seed": 42,
}

# Exact GPU-dep pins for the Modal training image. transformers==5.12.1 is
# the version stamped in the model's own config.json ("transformers_version")
# and ships src/transformers/models/qwen3_5/modeling_qwen3_5.py (verified at
# tag v5.12.1); trl==1.14.0 requires transformers>=4.56.2 and natively folds
# an `assistant_masks` dataset column into labels. All versions verified
# present on PyPI 2026-09-29.
TRAIN_IMAGE_PACKAGES = [
    "torch==2.14.0",
    "transformers==5.12.1",
    "trl==1.14.0",
    "peft==0.21.0",
    "accelerate==1.15.0",
    "datasets==5.0.1",
    "huggingface_hub==1.9.2",
]

_THINK_PREFIX_RE = re.compile(r"\A<think>\n(.*?)\n</think>(?:\n\n|\n)?(.*)\Z", re.DOTALL)


class SftError(Exception):
    """Loud failure: broken contract, broken mask invariant, or bad data."""


# ---------------------------------------------------------------------------
# Export verification (mirrors src/evallab/sft_tinker.py verify/load shape)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def load_conversations(path: Path) -> list[list[dict[str, Any]]]:
    """Strictly validate ``conversations.jsonl`` rows (role/content only)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SftError(f"cannot read {path}: {exc}") from exc
    conversations: list[list[dict[str, Any]]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise SftError(f"{path}:{number} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or set(row) != {"messages"}:
            raise SftError(f"{path}:{number} must hold exactly a 'messages' key")
        messages = row["messages"]
        if not isinstance(messages, list) or not messages:
            raise SftError(f"{path}:{number} 'messages' must be a non-empty list")
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise SftError(f"{path}:{number} messages need exactly role/content")
            if message["role"] not in ALLOWED_ROLES:
                raise SftError(f"{path}:{number} unsupported role {message['role']!r}")
            if not isinstance(message["content"], str):
                raise SftError(f"{path}:{number} content must be a string")
        conversations.append(messages)
    if not conversations:
        raise SftError(f"{path} contains no conversations")
    return conversations


@dataclass
class Export:
    """A verified export dir plus the digests the receipt must record."""

    data_dir: Path
    conversations: list[list[dict[str, Any]]]
    manifest: dict[str, Any]
    manifest_sha256: str
    conversations_sha256: str
    split_manifest_digest: str | None


def verify_export(data_dir: Path) -> Export:
    """Refuse anything that is not an ``evallab.sft_terminus/1`` export."""
    manifest_path = data_dir / MANIFEST_FILE
    conv_path = data_dir / CONVERSATIONS_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SftError(f"cannot read {manifest_path}: {exc}") from exc
    if manifest.get("contract") != EXPORT_CONTRACT:
        raise SftError(
            f"{manifest_path} contract {manifest.get('contract')!r} != {EXPORT_CONTRACT!r}"
        )
    expected = manifest.get("conversations_sha256")
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise SftError(f"{manifest_path} lacks a sha256 conversations_sha256")
    actual = _sha256_file(conv_path) if conv_path.is_file() else "<missing>"
    if actual != expected:
        raise SftError(
            f"digest mismatch: manifest pins {expected} but {conv_path} hashes to {actual}"
        )
    split = manifest.get("split_manifest") or {}
    split_digest = split.get("manifest_digest")
    return Export(
        data_dir=data_dir,
        conversations=load_conversations(conv_path),
        manifest=manifest,
        manifest_sha256=_sha256_file(manifest_path),
        conversations_sha256=actual,
        split_manifest_digest=split_digest if isinstance(split_digest, str) else None,
    )


# ---------------------------------------------------------------------------
# Rendering + assistant-only masks (shared by dry-run and Modal training)
# ---------------------------------------------------------------------------


def split_think_prefix(content: str) -> tuple[str | None, str]:
    """Split a leading ``<think>...</think>`` block (export reasoning policy).

    Returns ``(reasoning, body)``; ``(None, content)`` when no leading block
    is present. Only the leading block is special: any other ``<think>``
    text stays verbatim in the body.
    """
    match = _THINK_PREFIX_RE.match(content)
    if match is None:
        return None, content
    return match.group(1), match.group(2)


def to_template_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map export rows onto the fields the distill template renders.

    Assistant ``<think>`` prefixes become ``reasoning_content`` (the only
    field the template renders inside ``<think>``); every other assistant
    turn omits it and renders an empty ``<think></think>``. Tool calls stay
    verbatim ``<tool_call><function=...>`` text inside ``content`` — the
    export carries no structured ``tool_calls`` field (parsed-step
    trajectories are excluded upstream), so the template's structured
    branch is never triggered.
    """
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] != "assistant":
            rendered.append({"role": message["role"], "content": message["content"]})
            continue
        reasoning, body = split_think_prefix(message["content"])
        turn: dict[str, Any] = {"role": "assistant", "content": body}
        if reasoning is not None:
            turn["reasoning_content"] = reasoning
        rendered.append(turn)
    return rendered


def get_tokenizer(tokenizer_dir: str | None = None) -> Any:
    """Load the distill tokenizer (tokenizer files only, never weights)."""
    from transformers import AutoTokenizer

    source = tokenizer_dir or MODEL_ID
    kwargs: dict[str, Any] = {"trust_remote_code": False}
    if tokenizer_dir is None:
        kwargs["revision"] = MODEL_REVISION
    return AutoTokenizer.from_pretrained(source, **kwargs)


@dataclass
class Rendered:
    input_ids: list[int]
    mask: list[int]  # 1 = assistant-generated (trained), 0 = masked (-100)
    assistant_turns: int
    tokens_before: int
    truncated: bool
    dropped: bool

    @property
    def trained_before(self) -> int:
        return sum(self.mask) if not self.truncated else -1

    @property
    def trained_after(self) -> int:
        return 0 if self.dropped else sum(self.mask)


def render_and_mask(
    tokenizer: Any,
    messages: list[dict[str, Any]],
    *,
    max_length: int,
    label: str,
) -> Rendered:
    """Render with the model's own template; derive verified assistant spans.

    Primary signal: the template's ``{% generation %}`` markers via
    ``return_assistant_tokens_mask``. Verification: incremental prefix
    rendering — every prefix must be an exact token-prefix of the full
    render, and the union of assistant-turn suffixes must equal the template
    mask. Anything else raises :class:`SftError` naming the conversation.
    ``enable_thinking=True`` matches serving; with ``add_generation_prompt``
    unset it changes no training token, only the (absent) trailing prompt.
    """
    tmpl = to_template_messages(messages)
    full = tokenizer.apply_chat_template(
        tmpl,
        tokenize=True,
        add_generation_prompt=False,
        enable_thinking=True,
        return_assistant_tokens_mask=True,
    )
    ids = list(full["input_ids"])
    mask = [int(bit) for bit in full["assistant_masks"]]
    assistant_turns = sum(1 for message in tmpl if message["role"] == "assistant")

    # Verified prefix property + cross-check against the template markers.
    derived = [0] * len(ids)
    previous = 0
    for end in range(1, len(tmpl) + 1):
        part = list(
            tokenizer.apply_chat_template(
                tmpl[:end],
                tokenize=True,
                add_generation_prompt=False,
                enable_thinking=True,
            )["input_ids"]
        )
        if len(part) <= previous or part != ids[: len(part)]:
            raise SftError(f"{label}: prefix property broken at message {end}")
        if tmpl[end - 1]["role"] == "assistant":
            for pos in range(previous, len(part)):
                derived[pos] = 1
        previous = len(part)
    if previous != len(ids):
        raise SftError(f"{label}: prefix render diverged from full render")
    if derived != mask:
        raise SftError(f"{label}: template mask disagrees with prefix spans")
    if not any(mask):
        raise SftError(f"{label}: conversation has zero trainable tokens")

    tokens_before = len(ids)
    truncated = tokens_before > max_length
    if truncated:
        ids = ids[:max_length]
        mask = mask[:max_length]
    return Rendered(
        input_ids=ids,
        mask=mask,
        assistant_turns=assistant_turns,
        tokens_before=tokens_before,
        truncated=truncated,
        dropped=(sum(mask) == 0),
    )


@dataclass
class DryRun:
    export: Export
    renders: list[Rendered]
    max_length: int
    epochs: int
    example_index: int

    @property
    def kept(self) -> list[Rendered]:
        return [item for item in self.renders if not item.dropped]

    @property
    def trained_tokens(self) -> int:
        return sum(item.trained_after for item in self.kept)

    @property
    def total_tokens(self) -> int:
        return sum(len(item.input_ids) for item in self.kept)


def run_dry_run(
    tokenizer: Any,
    export: Export,
    *,
    max_length: int,
    epochs: int,
    example_index: int,
) -> DryRun:
    """Render every conversation; fail loudly on the first broken one."""
    renders = [
        render_and_mask(tokenizer, messages, max_length=max_length, label=f"conversation {index}")
        for index, messages in enumerate(export.conversations)
    ]
    return DryRun(
        export=export,
        renders=renders,
        max_length=max_length,
        epochs=epochs,
        example_index=example_index,
    )


def estimate_cost_usd(train_tokens_total: int) -> dict[str, float]:
    """A100-80GB time estimate from dry-run trained tokens (labeled guess)."""
    seconds = train_tokens_total / ASSUMED_TRAIN_THROUGHPUT_TOK_S
    hours = seconds / 3600.0
    return {
        "train_tokens_total": float(train_tokens_total),
        "assumed_throughput_tok_s": ASSUMED_TRAIN_THROUGHPUT_TOK_S,
        "seconds": seconds,
        "hours": hours,
        "usd_per_hour": MODAL_USD_PER_HOUR,
        "usd": hours * MODAL_USD_PER_HOUR,
    }


def print_dry_run(dry: DryRun, tokenizer: Any) -> None:
    """Human-readable dry-run report with one decoded masked view."""
    export = dry.export
    print(f"export: {export.data_dir}")
    print(f"  contract: {export.manifest.get('contract')}")
    print(f"  manifest_sha256: {export.manifest_sha256}")
    print(f"  conversations_sha256: {export.conversations_sha256}")
    print(f"  split_manifest_digest: {export.split_manifest_digest}")
    print(f"  conversations: {len(dry.renders)}  max_length: {dry.max_length}")
    print("per-conversation:")
    for index, item in enumerate(dry.renders):
        trained = item.trained_after
        flag = "TRUNCATED" if item.truncated else ("DROPPED" if item.dropped else "ok")
        print(
            f"  [{index}] msgs={len(export.conversations[index])} "
            f"asst_turns={item.assistant_turns} "
            f"tokens={item.tokens_before}->{len(item.input_ids)} "
            f"trained={trained} {flag}"
        )
    kept = len(dry.kept)
    truncated = sum(1 for item in dry.renders if item.truncated)
    dropped = sum(1 for item in dry.renders if item.dropped)
    print(
        f"totals: kept={kept}/{len(dry.renders)} tokens={dry.total_tokens} "
        f"trained={dry.trained_tokens} truncated={truncated} dropped={dropped}"
    )
    if dropped:
        print("  NOTE: dropped conversations contribute no loss (fully masked).")

    index = dry.example_index
    if index >= len(dry.renders):
        raise SftError(f"--example-index {index} out of range")
    item = dry.renders[index]
    print(f"--- decoded masked view: conversation {index} (trained spans) ---")
    full_ids = item.input_ids
    pos = 0
    span_no = 0
    while pos < len(full_ids):
        if item.mask[pos]:
            span_no += 1
            end = pos
            while end < len(full_ids) and item.mask[end]:
                end += 1
            print(f"  [trained span {span_no}: {end - pos} tokens]")
            print(tokenizer.decode(full_ids[pos:end]))
            pos = end
        else:
            pos += 1
    print("--- end masked view ---")

    total = dry.trained_tokens * dry.epochs
    quote = estimate_cost_usd(total)
    print(
        f"cost estimate: {dry.trained_tokens} trained-tokens x {dry.epochs} epochs "
        f"= {total} tokens / {ASSUMED_TRAIN_THROUGHPUT_TOK_S:g} tok/s (ASSUMED throughput) "
        f"= {quote['hours']:.3f} h x ${MODAL_USD_PER_HOUR:.6f}/h "
        f"= ${quote['usd']:.2f} (A100-80GB time only; +5-min idle tail per warm period)"
    )


def build_receipt(
    dry: DryRun,
    *,
    lora_rank: int,
    lora_alpha: int,
    learning_rate: float,
    epochs: int,
    run_name: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Training manifest: everything needed to reproduce or audit the run."""
    import transformers

    total = dry.trained_tokens * epochs
    receipt: dict[str, Any] = {
        "tool": "tools/modal-mimo-sft/sft.py",
        "model": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "run_name": run_name,
        "export": {
            "manifest_sha256": dry.export.manifest_sha256,
            "conversations_sha256": dry.export.conversations_sha256,
            "split_manifest_digest": dry.export.split_manifest_digest,
            "conversations": len(dry.renders),
            "kept": len(dry.kept),
        },
        "render": {
            "template": "model-own chat_template.jinja via apply_chat_template",
            "transformers": transformers.__version__,
            "enable_thinking": True,
            "think_mapping": "<think> content prefix -> reasoning_content field",
            "tool_calls": "verbatim <tool_call><function=...> text in content",
            "mask": "assistant_masks (template generation markers), "
            "prefix-property verified per conversation",
        },
        "train": {
            "trainer": "trl.SFTTrainer on pre-rendered input_ids+assistant_masks",
            "max_length": dry.max_length,
            "truncation": TRUNCATION_POLICY,
            "dtype": "bfloat16",
            "gradient_checkpointing": True,
            "packing": False,
            "lora": {
                "rank": lora_rank,
                "alpha": lora_alpha,
                "dropout": DEFAULTS["lora_dropout"],
                "target_modules": list(DEFAULT_LORA_TARGET_MODULES),
            },
            "sft": {
                "learning_rate": learning_rate,
                "lr_scheduler": DEFAULTS["lr_scheduler"],
                "warmup_ratio": DEFAULTS["warmup_ratio"],
                "num_epochs": epochs,
                "per_device_batch": DEFAULTS["per_device_batch"],
                "grad_accum_steps": DEFAULTS["grad_accum_steps"],
                "seed": DEFAULTS["seed"],
            },
            "image_packages": list(TRAIN_IMAGE_PACKAGES),
            "weights_volume": WEIGHTS_VOLUME,
            "output_volume": SFT_VOLUME,
        },
        "dry_run": {
            "tokens": dry.total_tokens,
            "trained_tokens": dry.trained_tokens,
            "truncated": sum(1 for item in dry.renders if item.truncated),
            "dropped": sum(1 for item in dry.renders if item.dropped),
        },
        "cost_estimate": estimate_cost_usd(total),
        "serve_hint": {
            "adapter": f"--enable-lora --lora-paths mimo_sft={SFT_MOUNT}/{run_name}/adapter",
            "targets_flag": "--lora-target-modules " + " ".join(DEFAULT_LORA_TARGET_MODULES),
        },
    }
    if extra:
        receipt.update(extra)
    return receipt


# ---------------------------------------------------------------------------
# Modal app (nothing here executes until train/merge pass --confirm-spend)
# ---------------------------------------------------------------------------

train_image = modal.Image.debian_slim(python_version="3.12").pip_install(*TRAIN_IMAGE_PACKAGES)
weights_volume = modal.Volume.from_name(WEIGHTS_VOLUME, create_if_missing=True)
sft_volume = modal.Volume.from_name(SFT_VOLUME, create_if_missing=True)
app = modal.App(APP_NAME)


@app.function(
    image=train_image,
    gpu="A100-80GB",
    cpu=4.0,
    memory=16 * 1024,
    volumes={WEIGHTS_MOUNT: weights_volume, SFT_MOUNT: sft_volume},
    timeout=6 * 3600,
)
def train_remote(export_rel: str, run_name: str, config: dict[str, Any]) -> dict[str, Any]:
    """LoRA SFT on one A100-80GB; writes adapter + receipt to the SFT volume."""
    import torch
    from datasets import Dataset
    from peft import LoraConfig, TaskType
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer

    out_dir = Path(SFT_MOUNT) / run_name
    if not Path(BASE_DIR, "config.json").is_file():
        raise RuntimeError(
            f"base weights missing at {BASE_DIR}; run "
            "tools/modal-mimo-serve/serve.py::download_weights first"
        )
    tokenizer = AutoTokenizer.from_pretrained(BASE_DIR, trust_remote_code=False, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    rows: list[dict[str, Any]] = []
    data_root = Path(SFT_MOUNT) / export_rel
    export = verify_export(data_root)
    for index, messages in enumerate(export.conversations):
        rendered = render_and_mask(
            tokenizer,
            messages,
            max_length=int(config["max_length"]),
            label=f"conversation {index}",
        )
        if rendered.dropped:
            continue
        rows.append({"input_ids": rendered.input_ids, "assistant_masks": rendered.mask})
    if not rows:
        raise RuntimeError("no trainable conversations after masking/truncation")
    dataset = Dataset.from_list(rows)

    model = AutoModelForCausalLM.from_pretrained(
        BASE_DIR,
        torch_dtype=torch.bfloat16,
        trust_remote_code=False,
    )
    model.config.use_cache = False
    peft_config = LoraConfig(
        r=int(config["lora_rank"]),
        lora_alpha=int(config["lora_alpha"]),
        lora_dropout=float(config["lora_dropout"]),
        bias="none",
        task_type=TaskType.CAUSAL_LM,
        target_modules=list(config["target_modules"]),
    )
    args = SFTConfig(
        output_dir=str(out_dir / "adapter"),
        max_length=int(config["max_length"]),
        truncation_mode="keep_start",
        packing=False,
        per_device_train_batch_size=int(config["per_device_batch"]),
        gradient_accumulation_steps=int(config["grad_accum_steps"]),
        num_train_epochs=float(config["num_epochs"]),
        learning_rate=float(config["learning_rate"]),
        lr_scheduler_type=str(config["lr_scheduler"]),
        warmup_ratio=float(config["warmup_ratio"]),
        bf16=True,
        gradient_checkpointing=True,
        logging_steps=10,
        save_steps=100,
        save_total_limit=2,
        seed=int(config["seed"]),
        report_to="none",
    )
    trainer = SFTTrainer(
        model=model,
        args=args,
        train_dataset=dataset,
        processing_class=tokenizer,
        peft_config=peft_config,
    )
    trainer.train()
    trainer.save_model(str(out_dir / "adapter"))
    receipt = dict(config["receipt"])
    receipt["remote"] = {
        "train_rows": len(rows),
        "adapter": f"{run_name}/adapter",
        "torch": torch.__version__,
    }
    (out_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    sft_volume.commit()
    return receipt


@app.function(
    image=train_image,
    gpu="A100-80GB",
    cpu=4.0,
    memory=16 * 1024,
    volumes={WEIGHTS_MOUNT: weights_volume, SFT_MOUNT: sft_volume},
    timeout=3600,
)
def merge_remote(adapter_rel: str, merged_rel: str) -> dict[str, Any]:
    """Merge a trained adapter into bf16 base weights (fallback path)."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not Path(BASE_DIR, "config.json").is_file():
        raise RuntimeError(
            f"base weights missing at {BASE_DIR}; run "
            "tools/modal-mimo-serve/serve.py::download_weights first"
        )
    adapter_dir = Path(SFT_MOUNT) / adapter_rel
    if not (adapter_dir / "adapter_config.json").is_file():
        raise RuntimeError(f"adapter missing at {adapter_dir}")
    model = AutoModelForCausalLM.from_pretrained(
        BASE_DIR, torch_dtype=torch.bfloat16, trust_remote_code=False
    )
    model = PeftModel.from_pretrained(model, str(adapter_dir))
    merged = model.merge_and_unload()
    out_dir = Path(SFT_MOUNT) / merged_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(str(out_dir), safe_serialization=True)
    AutoTokenizer.from_pretrained(BASE_DIR, trust_remote_code=False).save_pretrained(str(out_dir))
    sft_volume.commit()
    return {
        "adapter": adapter_rel,
        "merged": merged_rel,
        "torch": torch.__version__,
        "serve_hint": f"point a serve copy at {merged_rel} "
        "(same layout as the weights volume: <model-id>/<revision>/)",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _upload_export(data_dir: Path, dest_rel: str) -> None:
    volume = modal.Volume.from_name(SFT_VOLUME, create_if_missing=True)
    async with volume.batch_upload() as batch:
        await batch.put_directory(str(data_dir), dest_rel, recursive=True)


def _resolve_hparams(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "lora_rank": args.rank,
        "lora_alpha": args.alpha,
        "lora_dropout": DEFAULTS["lora_dropout"],
        "learning_rate": args.lr,
        "lr_scheduler": DEFAULTS["lr_scheduler"],
        "warmup_ratio": DEFAULTS["warmup_ratio"],
        "num_epochs": args.epochs,
        "per_device_batch": DEFAULTS["per_device_batch"],
        "grad_accum_steps": DEFAULTS["grad_accum_steps"],
        "max_length": args.max_length,
        "seed": DEFAULTS["seed"],
        "target_modules": list(DEFAULT_LORA_TARGET_MODULES),
    }


def cmd_dry_run(args: argparse.Namespace) -> int:
    export = verify_export(args.data)
    tokenizer = get_tokenizer(args.tokenizer_dir)
    dry = run_dry_run(
        tokenizer,
        export,
        max_length=args.max_length,
        epochs=args.epochs,
        example_index=args.example_index,
    )
    print_dry_run(dry, tokenizer)
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    export = verify_export(args.data)
    tokenizer = get_tokenizer(args.tokenizer_dir)
    dry = run_dry_run(
        tokenizer, export, max_length=args.max_length, epochs=args.epochs, example_index=0
    )
    params = _resolve_hparams(args)
    run_name = args.run_name or f"run-{export.manifest_sha256[7:15]}"
    receipt = build_receipt(
        dry,
        lora_rank=params["lora_rank"],
        lora_alpha=params["lora_alpha"],
        learning_rate=params["learning_rate"],
        epochs=params["num_epochs"],
        run_name=run_name,
    )
    params["receipt"] = receipt
    quote = receipt["cost_estimate"]
    print(
        f"train estimate: {quote['train_tokens_total']:.0f} tokens "
        f"= {quote['hours']:.3f} h x ${quote['usd_per_hour']:.6f}/h "
        f"= ${quote['usd']:.2f} on one A100-80GB "
        f"(assumed {quote['assumed_throughput_tok_s']:g} tok/s; "
        f"run {run_name} -> volume {SFT_VOLUME})"
    )
    if not args.confirm_spend:
        print(
            "REFUSED: train needs --confirm-spend. Nothing was uploaded, "
            "no Modal container was started, and no money was spent.",
            file=sys.stderr,
        )
        return 2
    dest_rel = f"uploads/{export.manifest_sha256[7:]}"
    asyncio.run(_upload_export(args.data, dest_rel))
    with app.run():
        result = train_remote.remote(dest_rel, run_name, params)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    merged_rel = args.out or f"{args.adapter.rstrip('/')}-merged"
    print(
        f"merge estimate: adapter {args.adapter} -> {merged_rel} on one A100-80GB "
        f"(minutes of load/merge/save; billed per second at ${MODAL_USD_PER_HOUR:.6f}/h "
        f"plus the 5-min idle tail)"
    )
    if not args.confirm_spend:
        print(
            "REFUSED: merge needs --confirm-spend. "
            "No Modal container was started, and no money was spent.",
            file=sys.stderr,
        )
        return 2
    with app.run():
        result = merge_remote.remote(args.adapter, merged_rel)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="$0 offline render + mask + cost check")
    dry.add_argument("--data", type=Path, required=True, help="export dir")
    dry.add_argument("--max-length", type=int, default=DEFAULT_MAX_LENGTH)
    dry.add_argument("--epochs", type=int, default=1)
    dry.add_argument("--example-index", type=int, default=0)
    dry.add_argument("--tokenizer-dir", default=None)
    dry.set_defaults(func=cmd_dry_run)

    train = sub.add_parser("train", help="gated LoRA SFT on one A100-80GB")
    train.add_argument("--data", type=Path, required=True, help="export dir")
    train.add_argument("--run-name", default=None)
    train.add_argument("--rank", type=int, default=DEFAULTS["lora_rank"])
    train.add_argument("--alpha", type=int, default=DEFAULTS["lora_alpha"])
    train.add_argument("--lr", type=float, default=DEFAULTS["learning_rate"])
    train.add_argument("--epochs", type=int, default=DEFAULTS["num_epochs"])
    train.add_argument("--grad-accum", type=int, default=DEFAULTS["grad_accum_steps"])
    train.add_argument("--max-length", type=int, default=DEFAULT_MAX_LENGTH)
    train.add_argument("--tokenizer-dir", default=None)
    train.add_argument("--confirm-spend", action="store_true")
    train.set_defaults(func=cmd_train)

    merge = sub.add_parser("merge", help="gated adapter->merged bf16 weights")
    merge.add_argument("--adapter", required=True, help="volume-relative adapter dir")
    merge.add_argument("--out", default=None, help="volume-relative merged dir")
    merge.add_argument("--confirm-spend", action="store_true")
    merge.set_defaults(func=cmd_merge)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
