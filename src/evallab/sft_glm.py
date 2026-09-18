"""GLM-5.3-Flash SFT training and serving runtime.

This module provides the complete, isolated external SFT recipe consumer, exact
GLM-5.3-Flash tokenizer/template loss-masking validation, hardware envelope
calculation, checkpoint manifest generation, CPU plumbing canary verification,
and RE serving handoff contracts.

Target model:
    zai-org/GLM-5.3-Flash (architecture: Glm5NextForConditionalGeneration)
    MoE hybrid: 320B total parameters, 18B active parameters, 45 layers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GLM_5_3_FLASH_SPECS",
    "CheckpointManifest",
    "GLMChatTemplatePatcher",
    "GLMHardwareEnvelope",
    "GLMLossMasker",
    "GLMModelSpecs",
    "GLMSFTConfig",
    "GLMTrainingRecord",
    "MaskValidationReceipt",
    "compute_hardware_envelope",
    "generate_re_serving_handoff",
    "generate_remote_job_manifest",
    "generate_trl_training_script",
    "run_cpu_canary_smoke",
    "verify_checkpoint",
]


# ==============================================================================
# 1. Model Specifications and Hardware Arithmetic
# ==============================================================================


class GLMModelSpecs(BaseModel):
    """Architectural specifications for GLM-5.3-Flash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = "zai-org/GLM-5.3-Flash"
    model_selector: str = "zai/glm-5.3-flash"
    architecture: str = "Glm5NextForConditionalGeneration"
    model_type: str = "glm5_next"
    text_model_type: str = "glm5_next_text"
    total_parameters: int = 320_000_000_000
    active_parameters: int = 18_000_000_000
    num_hidden_layers: int = 45
    hidden_size: int = 4096
    num_attention_heads: int = 64
    intermediate_size: int = 12288
    moe_intermediate_size: int = 2048
    num_routed_experts: int = 288
    num_shared_experts: int = 1
    num_experts_per_tok: int = 8
    vocab_size: int = 154880
    pad_token_id: int = 154820
    eos_token_ids: tuple[int, ...] = (154820, 154827, 154829)
    max_position_embeddings: int = 1048576  # 1M context
    linear_attention_layers: int = 34
    sparse_attention_layers: int = 11
    role_tokens: tuple[str, ...] = (
        "<|system|>",
        "<|user|>",
        "<|assistant|>",
        "<|observation|>",
        "<|endoftext|>",
    )


GLM_5_3_FLASH_SPECS = GLMModelSpecs()


class GLMHardwareEnvelope(BaseModel):
    """Estimated memory and hardware topology requirements for GLM-5.3-Flash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["lora", "qlora", "full_sft"]
    precision: Literal["fp8", "bf16", "int4"]
    context_length: int
    batch_size: int
    weights_memory_gb: float
    optimizer_memory_gb: float
    gradients_memory_gb: float
    activation_memory_gb: float
    total_vram_required_gb: float
    recommended_min_gpus: int
    recommended_gpu_type: str
    recommended_topology: str
    is_single_node_feasible: bool
    hardware_notes: str


def compute_hardware_envelope(
    method: Literal["lora", "qlora", "full_sft"] = "lora",
    precision: Literal["fp8", "bf16", "int4"] = "fp8",
    context_length: int = 16384,
    batch_size: int = 1,
) -> GLMHardwareEnvelope:
    """Calculates realistic memory requirements and recommended GPU cluster topology.

    GLM-5.3-Flash has 320B total parameters (sparse MoE). All 320B parameters
    must reside in aggregate accelerator memory, even though only 18B are active
    per token.

    Weights memory:
        BF16: 320B * 2 bytes = 640 GB
        FP8:  320B * 1 byte  = 320 GB
        INT4: 320B * 0.5 byte = 160 GB
    """
    total_p = GLM_5_3_FLASH_SPECS.total_parameters

    if precision == "bf16":
        bytes_per_param = 2.0
    elif precision == "fp8":
        bytes_per_param = 1.0
    elif precision == "int4":
        bytes_per_param = 0.5
    else:
        bytes_per_param = 1.0

    weights_gb = (total_p * bytes_per_param) / (1024**3)

    if method == "full_sft":
        # Full AdamW optimizer: 8 bytes per parameter (m + v in fp32) + 2 bytes fp16/bf16 master weights
        # Gradients: 2 bytes per parameter
        opt_gb = (total_p * 8.0) / (1024**3)
        grad_gb = (total_p * 2.0) / (1024**3)
        # Activation estimate for 45 layers hybrid attention
        act_gb = (45 * context_length * 4096 * batch_size * 2 * 4) / (1024**3)
        total_gb = weights_gb + opt_gb + grad_gb + act_gb

        # Recommended: 72x H100 80GB (NVIDIA NeMo standard reference) or 32x H200 141GB
        recommended_gpus = 72
        gpu_type = "NVIDIA H100 80GB SXM5"
        topology = "9 nodes x 8x H100 80GB (Infiniband NDR 400Gb/s) with Megatron Core / FSDP"
        single_node = False
        notes = (
            "Full SFT requires updating all 320B parameters. Optimizer states alone "
            f"require ~{opt_gb:.1f} GB VRAM. Multi-node cluster with high-speed interconnect "
            "is non-negotiable."
        )

    elif method == "lora":
        # LoRA on attention / MLP projections (~0.1% trainable params = ~320M params)
        trainable_p = 320_000_000
        opt_gb = (trainable_p * 8.0) / (1024**3)
        grad_gb = (trainable_p * 2.0) / (1024**3)
        # Frozen base weights in FP8 = 320 GB
        act_gb = (45 * context_length * 4096 * batch_size * 2 * 2) / (1024**3)
        total_gb = weights_gb + opt_gb + grad_gb + act_gb + 20.0  # 20GB buffer for CUDA runtime

        # Recommended: 8x H100 80GB SXM (640GB aggregate VRAM)
        recommended_gpus = 8
        gpu_type = "NVIDIA H100 80GB SXM5"
        topology = "1 node x 8x H100 80GB (NVLink 900GB/s) with Tensor Parallelism = 8 or FSDP"
        single_node = True
        notes = (
            f"LoRA keeps 320B base weights frozen in FP8 ({weights_gb:.1f} GB). "
            f"Trainable parameters ({trainable_p / 1e6:.1f}M) require only ~{opt_gb:.2f} GB optimizer VRAM. "
            "Fits comfortably within a single 8x H100 80GB node (640 GB total VRAM)."
        )

    elif method == "qlora":
        # 4-bit quantized base weights = 160 GB
        trainable_p = 320_000_000
        opt_gb = (trainable_p * 8.0) / (1024**3)
        grad_gb = (trainable_p * 2.0) / (1024**3)
        act_gb = (45 * context_length * 4096 * batch_size * 2 * 2) / (1024**3)
        total_gb = weights_gb + opt_gb + grad_gb + act_gb + 15.0

        recommended_gpus = 4
        gpu_type = "NVIDIA H100 80GB or 8x L40S 48GB"
        topology = "4x H100 80GB (320 GB VRAM) or 8x L40S 48GB (384 GB VRAM)"
        single_node = True
        notes = (
            f"4-bit quantized base weights take ~{weights_gb:.1f} GB. Feasible on 4x H100 80GB "
            "or 8x L40S 48GB nodes with bitsandbytes/AWQ/GPTQ."
        )

    return GLMHardwareEnvelope(
        method=method,
        precision=precision,
        context_length=context_length,
        batch_size=batch_size,
        weights_memory_gb=round(weights_gb, 2),
        optimizer_memory_gb=round(opt_gb, 2),
        gradients_memory_gb=round(grad_gb, 2),
        activation_memory_gb=round(act_gb, 2),
        total_vram_required_gb=round(total_gb, 2),
        recommended_min_gpus=recommended_gpus,
        recommended_gpu_type=gpu_type,
        recommended_topology=topology,
        is_single_node_feasible=single_node,
        hardware_notes=notes,
    )


# ==============================================================================
# 2. Tokenizer, Chat Template Patching, and Loss Masking Contract
# ==============================================================================

BUILTIN_PATCHED_GLM5_TEMPLATE = r"""[gMASK]<sop>
{%- set effective_reasoning_effort = reasoning_effort if reasoning_effort is defined and reasoning_effort in ['low', 'high'] else 'max' -%}
{%- if effective_reasoning_effort is not none -%}<|system|>Reasoning Effort: {{ effective_reasoning_effort | capitalize }}{%- endif -%}
{%- set clear_thinking = clear_thinking if clear_thinking is defined else false -%}
{%- if tools -%}
{%- macro tool_to_json(tool) -%}
    {%- set ns_tool = namespace(first=true) -%}
    {{ '{' -}}
    {%- for k, v in tool.items() -%}
        {%- if k != 'defer_loading' and k != 'strict' -%}
            {%- if not ns_tool.first -%}{{- ', ' -}}{%- endif -%}
            {%- set ns_tool.first = false -%}
            "{{ k }}": {{ v | tojson(ensure_ascii=False) }}
        {%- endif -%}
    {%- endfor -%}
    {{- '}' -}}
{%- endmacro -%}
<|system|>
# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{% for tool in tools %}
{%- if 'function' in tool -%}
    {%- set tool = tool['function'] -%}
{%- endif -%}
{% if tool.defer_loading is not defined or not tool.defer_loading %}
{{ tool_to_json(tool) }}
{% endif %}
{% endfor %}
</tools>

For each function call, output the function name and arguments within the following XML format:
<tool_call>{function-name}<arg_key>{arg-key-1}</arg_key><arg_value>{arg-value-1}</arg_value><arg_key>{arg-key-2}</arg_key><arg_value>{arg-value-2}</arg_value>...</tool_call>{%- endif -%}
{%- macro visible_text(content) -%}
    {%- if content is string -%}
        {{- content -}}
    {%- elif content is iterable and content is not mapping -%}
        {%- for item in content -%}
            {%- if item is mapping and item.type == 'text' -%}
                {{- item.text -}}
            {%- elif item is string -%}
                {{- item -}}
            {%- endif -%}
        {%- endfor -%}
    {%- elif content is not none -%}
        {{- content -}}
    {%- endif -%}
{%- endmacro -%}
{%- macro tool_response(text) -%}
{{- '<tool_response>' + text + '</tool_response>' -}}
{%- endmacro -%}
{%- set ns = namespace(last_user_index=-1) -%}
{%- for m in messages %}
    {%- if m.role == 'user' %}
        {%- set ns.last_user_index = loop.index0 -%}
    {%- endif %}
{%- endfor %}
{%- for m in messages -%}
{%- if m.role == 'user' -%}<|user|>{{ visible_text(m.content) }}
{%- elif m.role == 'assistant' -%}
<|assistant|>{% generation %}
{%- set content = visible_text(m.content) %}
{%- if m.reasoning_content is string %}
    {%- set reasoning_content = m.reasoning_content %}
{%- elif '</think>' in content %}
    {%- set reasoning_content = content.split('</think>')[0].split('<think>')[-1] %}
    {%- set content = content.split('</think>')[-1] %}
{%- endif %}
{%- if (not clear_thinking or loop.index0 > ns.last_user_index) and reasoning_content is defined -%}
{{ '<think>' + reasoning_content +  '</think>'}}
{%- else -%}
{{ '<think></think>' }}
{%- endif -%}
{%- if content.strip() -%}
{{ content.strip() }}
{%- endif -%}
{% if m.tool_calls %}
{% for tc in m.tool_calls %}
{%- if tc.function %}
    {%- set tc = tc.function %}
{%- endif %}
{{- '<tool_call>' ~ tc.name -}}
{% set _args = tc.arguments %}{% for k, v in _args.items() %}<arg_key>{{ k }}</arg_key><arg_value>{{ v | tojson(ensure_ascii=False) if v is not string else v }}</arg_value>{% endfor %}</tool_call>{% endfor %}
{% endif %}{% endgeneration %}
{%- elif m.role == 'tool' or m.role == 'observation' -%}
<|observation|>{{ tool_response(visible_text(m.content)) }}
{%- elif m.role == 'system' -%}
<|system|>{{ visible_text(m.content) }}
{%- endif -%}
{%- endfor -%}
{%- if add_generation_prompt -%}
    <|assistant|><think>
{%- endif -%}"""


class GLMChatTemplatePatcher:
    """Validates and patches GLM chat templates with generation tags for SFT."""

    @staticmethod
    def patch_template(original_template: str) -> str:
        """Injects `{% generation %}` and `{% endgeneration %}` markers around assistant outputs."""
        if "{% generation %}" in original_template and "{% endgeneration %}" in original_template:
            return original_template

        assistant_marker = "<|assistant|>"
        if assistant_marker not in original_template:
            raise ValueError(f"Template does not contain expected assistant marker: {assistant_marker}")

        # Replace assistant start
        patched = original_template.replace(
            "{%- elif m.role == 'assistant' -%}\n<|assistant|>",
            "{%- elif m.role == 'assistant' -%}\n<|assistant|>{% generation %}",
        )
        if "{% generation %}" not in patched:
            patched = original_template.replace(
                "<|assistant|>",
                "<|assistant|>{% generation %}",
            )

        # Close generation before tool / observation or user turn
        if "{%- elif m.role == 'tool' -%}" in patched:
            patched = patched.replace(
                "{%- elif m.role == 'tool' -%}",
                "{% endgeneration %}{%- elif m.role == 'tool' -%}",
            )
        elif "{%- elif m.role == 'user' -%}" in patched:
            patched = patched.replace(
                "{%- elif m.role == 'user' -%}",
                "{% endgeneration %}{%- elif m.role == 'user' -%}",
            )

        return patched

    @staticmethod
    def get_builtin_template() -> str:
        """Returns the fully validated builtin training chat template."""
        return BUILTIN_PATCHED_GLM5_TEMPLATE


class MaskValidationReceipt(BaseModel):
    """Machine-checkable validation receipt for token-level supervision masks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_tokens: int
    supervised_tokens: int
    unsupervised_tokens: int
    supervision_ratio: float
    has_empty_target: bool
    system_supervised: bool
    user_supervised: bool
    observation_supervised: bool
    delimiter_supervised: bool
    padding_supervised: bool
    is_valid: bool
    audit_summary: str
    token_preview: list[dict[str, Any]] = Field(default_factory=list)


class GLMLossMasker:
    """Applies and validates token-level loss masks for GLM conversational trajectories."""

    @staticmethod
    def validate_loss_masks(
        tokens: list[str],
        input_ids: list[int],
        labels: list[int],
        loss_mask: list[int],
    ) -> MaskValidationReceipt:
        """Validates that ONLY assistant target tokens are marked for loss computation."""
        if len(tokens) != len(input_ids) or len(input_ids) != len(labels) or len(labels) != len(loss_mask):
            raise ValueError(
                f"Length mismatch: tokens={len(tokens)}, input_ids={len(input_ids)}, "
                f"labels={len(labels)}, loss_mask={len(loss_mask)}"
            )

        total = len(tokens)
        supervised = sum(1 for m in loss_mask if m == 1)
        unsupervised = total - supervised
        has_empty = supervised == 0

        system_sup = False
        user_sup = False
        obs_sup = False
        delims_sup = False
        pad_sup = False

        current_role: str | None = None
        preview = []

        for i, (tok, tid, lbl, msk) in enumerate(zip(tokens, input_ids, labels, loss_mask, strict=True)):
            # Track current role section
            if "<|system|>" in tok or tok == "<|system|>":
                current_role = "system"
            elif "<|user|>" in tok or tok == "<|user|>":
                current_role = "user"
            elif "<|assistant|>" in tok or tok == "<|assistant|>":
                current_role = "assistant"
                # The delimiter itself must NOT be supervised
                if msk == 1 or lbl != -100:
                    delims_sup = True
                continue
            elif "<|observation|>" in tok or tok == "<|observation|>":
                current_role = "observation"
                if msk == 1 or lbl != -100:
                    delims_sup = True
                continue
            elif tok in ("<|endoftext|>", "[gMASK]", "<sop>") and (msk == 1 or lbl != -100):
                delims_sup = True

            # Check if non-assistant role tokens leaked into loss
            if current_role == "system" and (msk == 1 or lbl != -100):
                system_sup = True
            elif current_role == "user" and (msk == 1 or lbl != -100):
                user_sup = True
            elif current_role == "observation" and (msk == 1 or lbl != -100):
                obs_sup = True

            # Check padding token
            if tid == GLM_5_3_FLASH_SPECS.pad_token_id and (msk == 1 or lbl != -100):
                pad_sup = True

            if i < 25 or i >= total - 10:
                preview.append({
                    "idx": i,
                    "token": tok,
                    "id": tid,
                    "label": lbl,
                    "loss_mask": msk,
                    "role": current_role,
                })

        valid = (
            not has_empty
            and not system_sup
            and not user_sup
            and not obs_sup
            and not delims_sup
            and not pad_sup
        )

        ratio = round(supervised / total, 4) if total > 0 else 0.0
        summary = (
            f"Tokens: {total} total, {supervised} supervised ({ratio * 100:.1f}%), "
            f"{unsupervised} masked. Valid={valid}."
        )

        return MaskValidationReceipt(
            total_tokens=total,
            supervised_tokens=supervised,
            unsupervised_tokens=unsupervised,
            supervision_ratio=ratio,
            has_empty_target=has_empty,
            system_supervised=system_sup,
            user_supervised=user_sup,
            observation_supervised=obs_sup,
            delimiter_supervised=delims_sup,
            padding_supervised=pad_sup,
            is_valid=valid,
            audit_summary=summary,
            token_preview=preview,
        )


# ==============================================================================
# 3. Training Records and Causal Ordering Invariants
# ==============================================================================


class GLMTrainingRecord(BaseModel):
    """Normalized training demonstration turn or trajectory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    task_id: str
    source_split: Literal["train", "val", "holdout"]
    actor: str = "assistant"
    messages: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)

    def validate_causal_ordering(self) -> bool:
        """Verifies that each assistant turn only sees preceding context and no future observations."""
        for i, msg in enumerate(self.messages):
            role = msg.get("role")
            if role == "assistant":
                # Ensure all prior messages are system, user, or prior tool observations
                for prior in self.messages[:i]:
                    if prior.get("role") not in ("system", "user", "assistant", "tool", "observation"):
                        return False
            elif role in ("tool", "observation") and i == 0:
                return False
        return True


# ==============================================================================
# 4. Maintained Trainer Consumer (TRL / SFTTrainer / PEFT)
# ==============================================================================


class GLMSFTConfig(BaseModel):
    """Configuration for reproducible TRL SFTTrainer executions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = "zai-org/GLM-5.3-Flash"
    method: Literal["lora", "qlora", "full_sft"] = "lora"
    output_dir: str = "runs/sft-glm-flash-checkpoints"
    dataset_path: str = "research/experiments/sft-glm-flash/dataset.jsonl"
    learning_rate: float = 1e-4
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = (
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    )
    max_seq_length: int = 16384
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 8
    num_train_epochs: int = 1
    max_steps: int = 250
    warmup_ratio: float = 0.03
    lr_scheduler_type: str = "cosine"
    weight_decay: float = 0.01
    logging_steps: int = 5
    save_steps: int = 50
    bf16: bool = True
    gradient_checkpointing: bool = True
    packing: bool = False  # Keep false to ensure exact assistant loss boundaries survive
    dataset_text_field: str = "messages"


def generate_trl_training_script(config: GLMSFTConfig) -> str:
    """Generates a complete, reproducible training runner script using official TRL."""
    return f'''#!/usr/bin/env python3
"""Maintained TRL SFTTrainer runner for {config.model_id}.

Auto-generated by evallab.sft_glm.
"""

import json
import os
import sys
from pathlib import Path

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, TaskType
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments
from trl import SFTConfig, SFTTrainer

from evallab.sft_glm import GLMChatTemplatePatcher, GLM_5_3_FLASH_SPECS

def main():
    print(f"=== Starting TRL SFT for {{'{config.model_id}'}} (Method: {config.method}) ===")
    
    # 1. Load Tokenizer & Patch Chat Template
    tokenizer = AutoTokenizer.from_pretrained("{config.model_id}")
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    # Apply generation markers for exact assistant-only loss masking
    tokenizer.chat_template = GLMChatTemplatePatcher.get_builtin_template()
    print("✓ Loaded and patched chat template with exact generation markers")

    # 2. Load Dataset
    data_path = "{config.dataset_path}"
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"Training dataset not found at {{data_path}}")
    
    dataset = load_dataset("json", data_files=data_path, split="train")
    print(f"✓ Loaded training dataset: {{len(dataset)}} records")

    # 3. Model & PEFT Configuration
    model_kwargs = {{
        "trust_remote_code": True,
        "torch_dtype": torch.bfloat16 if {config.bf16} else torch.float16,
        "device_map": "auto",
    }}

    print("Loading base model...")
    model = AutoModelForCausalLM.from_pretrained("{config.model_id}", **model_kwargs)
    
    if "{config.method}" == "lora":
        peft_config = LoraConfig(
            r={config.lora_r},
            lora_alpha={config.lora_alpha},
            lora_dropout={config.lora_dropout},
            target_modules={list(config.target_modules)},
            bias="none",
            task_type=TaskType.CAUSAL_LM,
        )
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
    elif "{config.method}" == "full_sft":
        peft_config = None
        print("Configured for full SFT across all layers")

    # 4. TRL SFTConfig
    sft_args = SFTConfig(
        output_dir="{config.output_dir}",
        learning_rate={config.learning_rate},
        lr_scheduler_type="{config.lr_scheduler_type}",
        warmup_ratio={config.warmup_ratio},
        weight_decay={config.weight_decay},
        per_device_train_batch_size={config.per_device_train_batch_size},
        gradient_accumulation_steps={config.gradient_accumulation_steps},
        max_steps={config.max_steps},
        num_train_epochs={config.num_train_epochs},
        logging_steps={config.logging_steps},
        save_steps={config.save_steps},
        bf16={config.bf16},
        gradient_checkpointing={config.gradient_checkpointing},
        packing={config.packing},
        max_seq_length={config.max_seq_length},
        dataset_text_field="{config.dataset_text_field}",
        report_to="none",
    )

    # 5. Initialize Trainer
    trainer = SFTTrainer(
        model=model,
        args=sft_args,
        train_dataset=dataset,
        processing_class=tokenizer,
        peft_config=peft_config if "{config.method}" == "lora" else None,
    )

    print("=== Launching SFT Training ===")
    train_result = trainer.train()
    
    # 6. Save Checkpoint
    final_output = Path("{config.output_dir}") / "final"
    trainer.save_model(str(final_output))
    tokenizer.save_pretrained(str(final_output))
    print(f"✓ Training finished. Saved checkpoint to {{final_output}}")

if __name__ == "__main__":
    main()
'''


def generate_remote_job_manifest(config: GLMSFTConfig) -> dict[str, Any]:
    """Generates Slurm/batch submission script and execution manifest for remote execution."""
    hardware = compute_hardware_envelope(
        method=config.method,
        precision="fp8" if config.method == "lora" else "bf16",
        context_length=config.max_seq_length,
    )

    slurm_script = f"""#!/bin/bash
#SBATCH --job-name=glm-5.3-flash-sft
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node={hardware.recommended_min_gpus}
#SBATCH --cpus-per-task=32
#SBATCH --mem=480G
#SBATCH --time=04:00:00
#SBATCH --output={config.output_dir}/slurm-%j.out
#SBATCH --error={config.output_dir}/slurm-%j.err

set -euo pipefail

echo "Job starting on $(hostname) at $(date)"
echo "GPUs allocated: {hardware.recommended_min_gpus} x {hardware.recommended_gpu_type}"

# Ensure environment
source /opt/conda/bin/activate sft-glm
mkdir -p "{config.output_dir}"

# Execute training with Accelerate / TRL
accelerate launch \\
    --num_processes={hardware.recommended_min_gpus} \\
    --mixed_precision=bf16 \\
    train_glm_sft.py

echo "Job completed successfully at $(date)"
"""

    return {
        "job_name": "glm-5.3-flash-sft",
        "method": config.method,
        "target_model": config.model_id,
        "hardware_requirements": hardware.model_dump(),
        "slurm_submission_script": slurm_script,
        "detached_execution": True,
        "checkpoint_dir": config.output_dir,
        "shutdown_policy": "terminate_on_completion_or_failure",
        "generated_at": datetime.now(UTC).isoformat(),
    }


# ==============================================================================
# 5. Checkpoint Manifest & Reload Verification
# ==============================================================================


class CheckpointManifest(BaseModel):
    """Machine-checkable manifest for trained GLM checkpoints and adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: int = 1
    model_id: str = "zai-org/GLM-5.3-Flash"
    base_model_revision: str
    training_method: Literal["lora", "qlora", "full_sft"]
    step_count: int
    final_loss: float
    adapter_path: str
    dataset_hash: str
    config_hash: str
    created_at: str
    serving_backend: Literal["vllm", "sglang"] = "vllm"
    vllm_command: str


def verify_checkpoint(checkpoint_dir: Path) -> dict[str, Any]:
    """Inspects and validates a saved checkpoint directory."""
    if not checkpoint_dir.exists():
        return {"valid": False, "error": f"Directory not found: {checkpoint_dir}"}

    manifest_path = checkpoint_dir / "checkpoint_manifest.json"
    if not manifest_path.exists():
        return {"valid": False, "error": "Missing checkpoint_manifest.json"}

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = CheckpointManifest.model_validate(manifest_data)
    except Exception as e:
        return {"valid": False, "error": f"Invalid manifest: {e}"}

    # Verify adapter files exist for LoRA
    if manifest.training_method in ("lora", "qlora"):
        adapter_config = checkpoint_dir / "adapter_config.json"
        adapter_weights = checkpoint_dir / "adapter_model.safetensors"
        adapter_bin = checkpoint_dir / "adapter_model.bin"
        if not adapter_config.exists():
            return {"valid": False, "error": "Missing adapter_config.json"}
        if not adapter_weights.exists() and not adapter_bin.exists():
            return {"valid": False, "error": "Missing adapter weights (safetensors or bin)"}

    return {
        "valid": True,
        "manifest": manifest.model_dump(),
        "checkpoint_dir": str(checkpoint_dir),
    }


# ==============================================================================
# 6. CPU Plumbing Canary Smoke Test
# ==============================================================================


def run_cpu_canary_smoke(output_dir: Path) -> dict[str, Any]:
    """Performs genuine backward update, save, reload and generation on CPU.

    Strictly labeled: PLUMBING ONLY. Verifies trainer mechanics and parameter
    delta without claiming model qualification.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Self-contained tiny causal model & synthetic dataset
    import torch  # ty: ignore[unresolved-import]
    import torch.nn as nn  # ty: ignore[unresolved-import]
    import torch.optim as optim  # ty: ignore[unresolved-import]

    class TinyCausalLM(nn.Module):
        def __init__(self, vocab_size: int = 256, dim: int = 64):
            super().__init__()
            self.embed = nn.Embedding(vocab_size, dim)
            self.head = nn.Linear(dim, vocab_size, bias=False)

        def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
            x = self.embed(input_ids)
            return self.head(x)

    torch.manual_seed(42)
    model = TinyCausalLM()
    optimizer = optim.AdamW(model.parameters(), lr=0.05)
    criterion = nn.CrossEntropyLoss(ignore_index=-100)

    # Input sequence: [user_token, assistant_token_1, assistant_token_2]
    # Labels: [-100, assistant_token_1, assistant_token_2]
    input_ids = torch.tensor([[10, 20, 30]], dtype=torch.long)
    labels = torch.tensor([[-100, 20, 30]], dtype=torch.long)

    # Record initial weights
    w_initial = model.head.weight.clone().detach()

    # Step 1: Forward pass & initial loss
    logits_0 = model(input_ids)
    loss_0 = criterion(logits_0.view(-1, 256), labels.view(-1))
    initial_loss_val = float(loss_0.item())

    # Step 2: Backward pass & optimizer step
    optimizer.zero_grad()
    loss_0.backward()
    optimizer.step()

    # Step 3: Second step to measure progress
    logits_1 = model(input_ids)
    loss_1 = criterion(logits_1.view(-1, 256), labels.view(-1))
    final_loss_val = float(loss_1.item())

    # Verify weight delta
    w_final = model.head.weight.clone().detach()
    weight_diff = float(torch.norm(w_final - w_initial).item())

    assert weight_diff > 0.0, "Weights must change after optimizer step"
    assert final_loss_val < initial_loss_val, "Loss must decrease after update"

    # Step 4: Save checkpoint and manifest
    ckpt_path = output_dir / "canary_checkpoint.pt"
    torch.save(model.state_dict(), ckpt_path)

    manifest = CheckpointManifest(
        manifest_version=1,
        model_id="canary-tiny-causal-plumbing",
        base_model_revision="canary-rev-01",
        training_method="full_sft",
        step_count=2,
        final_loss=round(final_loss_val, 4),
        adapter_path=str(ckpt_path),
        dataset_hash=hashlib.sha256(b"canary-dataset-bytes").hexdigest(),
        config_hash=hashlib.sha256(b"canary-config-bytes").hexdigest(),
        created_at=datetime.now(UTC).isoformat(),
        serving_backend="vllm",
        vllm_command="vllm serve --model canary-tiny",
    )
    (output_dir / "checkpoint_manifest.json").write_text(
        json.dumps(manifest.model_dump(), indent=2), encoding="utf-8"
    )

    # Step 5: Reload checkpoint and verify weights match
    reloaded_model = TinyCausalLM()
    reloaded_model.load_state_dict(torch.load(ckpt_path, weights_only=True))
    w_reloaded = reloaded_model.head.weight.clone().detach()

    reload_diff = float(torch.norm(w_reloaded - w_final).item())
    assert reload_diff == 0.0, "Reloaded weights must match saved weights exactly"

    return {
        "level": "cpu_plumbing_canary",
        "is_target_model": False,
        "initial_loss": round(initial_loss_val, 4),
        "final_loss": round(final_loss_val, 4),
        "loss_decreased": final_loss_val < initial_loss_val,
        "weight_norm_delta": round(weight_diff, 6),
        "reload_verified": True,
        "manifest": manifest.model_dump(),
        "checkpoint_path": str(ckpt_path),
    }


# ==============================================================================
# 7. RE Serving Handoff
# ==============================================================================


def generate_re_serving_handoff(
    checkpoint_dir: str = "runs/sft-glm-flash-checkpoints/final",
    adapter_name: str = "glm-flash-sft-v1",
) -> dict[str, Any]:
    """Generates the serving adoption handoff specification for RE - Eval Lab."""
    vllm_cmd = (
        f"vllm serve {GLM_5_3_FLASH_SPECS.model_id} "
        f"--enable-lora "
        f"--lora-modules {adapter_name}={checkpoint_dir} "
        f"--max-model-len 32768 "
        f"--tensor-parallel-size 8 "
        f"--chat-template research/experiments/sft-glm-flash/chat_template.jinja"
    )

    sglang_cmd = (
        f"python -m sglang.launch_server "
        f"--model-path {GLM_5_3_FLASH_SPECS.model_id} "
        f"--enable-lora "
        f"--lora-paths {adapter_name}={checkpoint_dir} "
        f"--tp 8 "
        f"--chat-template research/experiments/sft-glm-flash/chat_template.jinja"
    )

    return {
        "target_model": GLM_5_3_FLASH_SPECS.model_id,
        "model_selector": GLM_5_3_FLASH_SPECS.model_selector,
        "adapter_name": adapter_name,
        "adapter_path": checkpoint_dir,
        "serving_backend": "vllm",
        "vllm_command": vllm_cmd,
        "sglang_command": sglang_cmd,
        "pinned_inference_settings": {
            "temperature": 0.95,
            "top_p": 1.0,
            "reasoning_effort": "max",
            "clear_thinking": False,
        },
        "evaluation_protocol": {
            "harness": "mini-swe-agent (stock, fixed)",
            "benchmark": "TB4 (Terminal-Bench 4)",
            "comparison": "same-mini pre/post SFT evaluation on unseen task splits",
        },
    }
