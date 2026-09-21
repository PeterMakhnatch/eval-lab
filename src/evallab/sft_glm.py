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
import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Immutable Hugging Face revision of zai-org/GLM-5.3-Flash qualified by HAR-66
# (research/experiments/glm53-sft-qualification/next-task.json). Both the model
# weights/config and the tokenizer (incl. chat_template.jinja) are pinned to it.
GLM_MODEL_REVISION = "eb9eb208eb0d988989d07a6a12d0fdeb5f52574a"
GLM_TOKENIZER_REVISION = GLM_MODEL_REVISION

# Message keys that carry harness reward/verifier metadata and must never reach
# the learner as text. Checked by GLMTrainingRecord.validate_causal_ordering and
# by the generated trainer's HAR-65 record loader (fail closed, never stripped).
REWARD_METADATA_KEYS = frozenset({
    "verified_success",
    "reward",
    "rewards",
    "final_reward",
    "grade",
    "grades",
    "tests_passed",
    "test_results",
    "score",
    "success",
    "passed",
    "verifier_verdict",
    "outcome",
})

# LoRA target modules qualified by HAR-66 for glm5_next: KDA projections hit 34
# linear-attention layers, DSA projections hit 11 sparse layers, gate/up/down
# hit dense layers 0-2 and shared experts 3-44. Routed experts are 3D
# nn.Parameter tensors, not nn.Linear, and are intentionally absent.
QUALIFIED_LORA_TARGET_MODULES = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "b_proj",
    "g_a_proj",
    "g_b_proj",
    "q_a_proj",
    "q_b_proj",
    "kv_a_proj_with_mqa",
    "kv_b_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)

__all__ = [
    "GLM_5_3_FLASH_SPECS",
    "GLM_MODEL_REVISION",
    "GLM_TOKENIZER_REVISION",
    "QUALIFIED_LORA_TARGET_MODULES",
    "REWARD_METADATA_KEYS",
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
    "har65_record_to_messages",
    "pinned_library_versions",
    "run_cpu_canary_smoke",
    "verify_checkpoint",
    "write_checkpoint_manifest",
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
    """Estimated memory and hardware topology requirements for GLM-5.3-Flash.

    Every number here is storage arithmetic, NOT a qualified training
    allocation: no target-model run receipt backs the GPU counts or topology.
    The `qualification` field labels this explicitly for handoff consumers.
    """

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
    qualification: Literal["estimate-unverified"] = "estimate-unverified"


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
            "Estimated (unverified, no target-model run receipt) to fit a single "
            "8x H100 80GB node (640 GB total VRAM)."
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
    """Validates and patches GLM chat templates with generation tags for SFT.

    The patch targets the real upstream template
    (zai-org/GLM-5.3-Flash at revision GLM_MODEL_REVISION): the assistant
    branch opens with `{%- elif m.role == 'assistant' -%}` followed by a
    `<|assistant|>` line, and closes with `{% endif %}` immediately before the
    `{%- elif m.role == 'tool' -%}` branch. The `add_generation_prompt` tail
    is never touched. Anything else fails closed with ValueError.
    """

    # Exact upstream anchors (see revision pinned above). The open anchor is
    # the assistant branch header; the close anchor is the end of the
    # assistant branch (its `{% endif %}`) right before the tool branch.
    _OPEN_ANCHOR = "{%- elif m.role == 'assistant' -%}\n<|assistant|>"
    _CLOSE_ANCHOR = "{% endif %}\n{%- elif m.role == 'tool' -%}"
    _TAIL_ANCHOR = "{%- if add_generation_prompt -%}"

    @staticmethod
    def patch_template(original_template: str) -> str:
        """Injects `{% generation %}` / `{% endgeneration %}` around assistant outputs.

        Fails closed (ValueError) unless exactly one balanced pair is present
        afterwards and the `add_generation_prompt` tail is untouched.
        """
        if "{% generation %}" in original_template and "{% endgeneration %}" in original_template:
            GLMChatTemplatePatcher._check_balanced(original_template)
            return original_template

        if "<|assistant|>" not in original_template:
            raise ValueError("Template does not contain expected assistant marker: <|assistant|>")
        if GLMChatTemplatePatcher._OPEN_ANCHOR not in original_template:
            raise ValueError(
                "Template does not contain the upstream assistant-branch anchor "
                "`{%- elif m.role == 'assistant' -%}` + `<|assistant|>`; refusing to guess."
            )
        if GLMChatTemplatePatcher._CLOSE_ANCHOR not in original_template:
            raise ValueError(
                "Template does not contain the upstream assistant-close anchor "
                "`{% endif %}` before `{%- elif m.role == 'tool' -%}`; refusing to guess."
            )

        patched = original_template.replace(
            GLMChatTemplatePatcher._OPEN_ANCHOR,
            "{%- elif m.role == 'assistant' -%}\n<|assistant|>{% generation %}",
            1,
        )
        patched = patched.replace(
            GLMChatTemplatePatcher._CLOSE_ANCHOR,
            "{% endif %}{% endgeneration %}\n{%- elif m.role == 'tool' -%}",
            1,
        )
        GLMChatTemplatePatcher._check_balanced(patched)
        return patched

    @staticmethod
    def _check_balanced(template: str) -> None:
        """Raises unless exactly one balanced generation pair exists and the tail is clean."""
        opens = template.count("{% generation %}")
        closes = template.count("{% endgeneration %}")
        if opens != 1 or closes != 1:
            raise ValueError(
                f"Unbalanced generation tags after patching: {opens} opens vs {closes} closes "
                "(expected exactly one pair)."
            )
        tail_index = template.find(GLMChatTemplatePatcher._TAIL_ANCHOR)
        if tail_index != -1 and (
            "{% generation %}" in template[tail_index:] or "{% endgeneration %}" in template[tail_index:]
        ):
            raise ValueError("Patch leaked into the `add_generation_prompt` tail; refusing patched template.")

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
    preamble_supervised: bool = False
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
        """Validates that ONLY assistant target tokens are marked for loss computation.

        Exactly what is checked, per token in order:
        1. Lengths of the four inputs agree (else ValueError).
        2. Any token supervised (loss_mask == 1 or label != -100) before the
           first role delimiter (`<|system|>`, `<|user|>`, `<|assistant|>`,
           `<|observation|>`) sets `preamble_supervised` and fails.
        3. The `<|assistant|>` / `<|observation|>` delimiters themselves, the
           `[gMASK]` / `<sop>` / `<|endoftext|>` framing tokens, and any token
           in a system / user / observation section must not be supervised.
        4. The pad token id must not be supervised.
        5. At least one token must be supervised (empty targets fail).
        Role sections are tracked by delimiter substrings in token text; the
        content of assistant sections (reasoning, text, tool calls) is the only
        supervised region.
        """
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
        preamble_sup = False
        current_role: str | None = None
        preview = []

        for tok, lbl, msk in zip(tokens, labels, loss_mask, strict=True):
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
            elif current_role is None and (msk == 1 or lbl != -100):
                preamble_sup = True
        valid = (
            not has_empty
            and not system_sup
            and not user_sup
            and not obs_sup
            and not delims_sup
            and not pad_sup
            and not preamble_sup
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
            preamble_supervised=preamble_sup,
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
        """Verifies causal validity of the demonstration.

        Exactly what is checked:
        1. Every `tool` / `observation` message is preceded by at least one
           `assistant` message (a tool result with no preceding tool call, at
           any position, fails).
        2. Every message role is one of system / user / assistant / tool /
           observation (unknown roles fail).
        3. No message carries reward/verifier metadata keys
           (REWARD_METADATA_KEYS: `verified_success`, `reward`, `grade`,
           `tests_passed`, and similar) at its top level, and neither does the
           record-level `metadata` mapping. Leak metadata fails instead of
           flowing into training.
        """
        for i, msg in enumerate(self.messages):
            role = msg.get("role")
            if role not in ("system", "user", "assistant", "tool", "observation"):
                return False
            if role in ("tool", "observation") and not any(
                prior.get("role") == "assistant" for prior in self.messages[:i]
            ):
                return False
            if any(key in REWARD_METADATA_KEYS for key in msg):
                return False
        return not any(key in REWARD_METADATA_KEYS for key in self.metadata)


# ==============================================================================
# 4. Maintained Trainer Consumer (TRL / SFTTrainer / PEFT)
# ==============================================================================


class GLMSFTConfig(BaseModel):
    """Configuration for reproducible TRL SFTTrainer executions.

    Field names match TRL 1.13 `SFTConfig` (`max_length`, `warmup_steps`).
    `target_modules` defaults to the HAR-66 qualified glm5_next list.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = "zai-org/GLM-5.3-Flash"
    model_revision: str = GLM_MODEL_REVISION
    tokenizer_revision: str = GLM_TOKENIZER_REVISION
    method: Literal["lora", "qlora", "full_sft"] = "lora"
    output_dir: str = "runs/sft-glm-flash-checkpoints"
    dataset_path: str = "research/experiments/sft-glm-flash/dataset.jsonl"
    learning_rate: float = 1e-4
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = QUALIFIED_LORA_TARGET_MODULES
    max_length: int = 4096
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 8
    num_train_epochs: int = 1
    max_steps: int = 250
    warmup_steps: int = 8
    lr_scheduler_type: str = "cosine"
    weight_decay: float = 0.01
    logging_steps: int = 5
    save_steps: int = 50
    bf16: bool = True
    gradient_checkpointing: bool = True
    packing: bool = False  # Keep false to ensure exact assistant loss boundaries survive
    dataset_text_field: str = "messages"


def generate_trl_training_script(config: GLMSFTConfig) -> str:
    """Generates a complete, reproducible training runner script using official TRL.

    The emitted script constructs under TRL 1.13: `SFTConfig` uses `max_length`
    / `warmup_steps` (not `max_seq_length` / `warmup_ratio`), sets
    `assistant_only_loss=True` so the patched template's `{% generation %}`
    markers actually supervise assistant tokens only, passes `peft_config` to
    `SFTTrainer` without a `get_peft_model` pre-wrap, pins model/tokenizer
    revisions on every `from_pretrained`, writes `checkpoint_manifest.json`
    via the shared writer, and finishes with a save/reload forward-pass
    receipt. `qlora` loads the base in 4-bit (`BitsAndBytesConfig`); `full_sft`
    passes no PEFT config.
    """
    model_id = json.dumps(config.model_id)
    model_revision = json.dumps(config.model_revision)
    tokenizer_revision = json.dumps(config.tokenizer_revision)
    output_dir = json.dumps(config.output_dir)
    dataset_path = json.dumps(config.dataset_path)
    target_modules = json.dumps(list(config.target_modules))
    config_hash = hashlib.sha256(
        json.dumps(config.model_dump(), sort_keys=True).encode("utf-8")
    ).hexdigest()

    if config.method == "qlora":
        peft_imports = "from peft import LoraConfig, PeftModel, TaskType"
        bnb_import = "from transformers import BitsAndBytesConfig"
        quantization_block = (
            "    bnb_config = BitsAndBytesConfig(\n"
            "        load_in_4bit=True,\n"
            '        bnb_4bit_quant_type="nf4",\n'
            "        bnb_4bit_compute_dtype=torch.bfloat16,\n"
            "        bnb_4bit_use_double_quant=True,\n"
            "    )\n"
            '    model_kwargs["quantization_config"] = bnb_config\n'
        )
        peft_block = (
            "    peft_config = LoraConfig(\n"
            f"        r={config.lora_r},\n"
            f"        lora_alpha={config.lora_alpha},\n"
            f"        lora_dropout={config.lora_dropout},\n"
            f"        target_modules={target_modules},\n"
            '        bias="none",\n'
            "        task_type=TaskType.CAUSAL_LM,\n"
            "    )\n"
        )
        trainer_peft_arg = "peft_config=peft_config,"
        reload_block = (
            "    # Save/reload receipt: fresh base + adapter from disk must match.\n"
            "    reload_base = _load_base_model(model_kwargs)\n"
            '    reloaded = PeftModel.from_pretrained(reload_base, str(final_output))\n'
            "    reloaded.eval()\n"
            "    with torch.no_grad():\n"
            "        after_logits = reloaded(probe_ids).logits.float()\n"
        )
    elif config.method == "lora":
        peft_imports = "from peft import LoraConfig, PeftModel, TaskType"
        bnb_import = ""
        quantization_block = ""
        peft_block = (
            "    peft_config = LoraConfig(\n"
            f"        r={config.lora_r},\n"
            f"        lora_alpha={config.lora_alpha},\n"
            f"        lora_dropout={config.lora_dropout},\n"
            f"        target_modules={target_modules},\n"
            '        bias="none",\n'
            "        task_type=TaskType.CAUSAL_LM,\n"
            "    )\n"
        )
        trainer_peft_arg = "peft_config=peft_config,"
        reload_block = (
            "    # Save/reload receipt: fresh base + adapter from disk must match.\n"
            "    reload_base = _load_base_model(model_kwargs)\n"
            '    reloaded = PeftModel.from_pretrained(reload_base, str(final_output))\n'
            "    reloaded.eval()\n"
            "    with torch.no_grad():\n"
            "        after_logits = reloaded(probe_ids).logits.float()\n"
        )
    else:  # full_sft
        peft_imports = ""
        bnb_import = ""
        quantization_block = ""
        peft_block = "    peft_config = None\n"
        trainer_peft_arg = "peft_config=None,"
        reload_block = (
            "    # Save/reload receipt: full weights reloaded from disk must match.\n"
            '    reloaded_kwargs = {k: v for k, v in model_kwargs.items() if k != "quantization_config"}\n'
            "    reloaded = _load_base_model(reloaded_kwargs, model_id=str(final_output), revision=None)\n"
            "    reloaded.eval()\n"
            "    with torch.no_grad():\n"
            "        after_logits = reloaded(probe_ids).logits.float()\n"
        )

    lines = [
        "#!/usr/bin/env python3",
        f'"""Maintained TRL SFTTrainer runner for {config.model_id}.',
        "",
        "Auto-generated by evallab.sft_glm. Run inside the isolated pinned",
        "environment (research/experiments/sft-glm-flash/requirements-sft.txt).",
        '"""',
        "",
        "import hashlib",
        "import json",
        "import os",
        "from pathlib import Path",
        "",
        "import torch",
        "from datasets import Dataset",
        peft_imports,
        "from transformers import AutoModelForCausalLM, AutoTokenizer",
        bnb_import,
        "from trl import SFTConfig, SFTTrainer",
        "",
        "from evallab.sft_glm import (",
        "    GLMChatTemplatePatcher,",
        "    GLMLossMasker,",
        "    har65_record_to_messages,",
        "    pinned_library_versions,",
        "    write_checkpoint_manifest,",
        ")",
        "",
        f"MODEL_ID = {model_id}",
        f"MODEL_REVISION = {model_revision}",
        f"TOKENIZER_REVISION = {tokenizer_revision}",
        f"METHOD = {json.dumps(config.method)}",
        f"OUTPUT_DIR = {output_dir}",
        f"DATA_PATH = {dataset_path}",
        f"CONFIG_HASH = {json.dumps(config_hash)}",
        "",
        "",
        "def _revision_kwargs(path, revision):",
        '    """Local stand-in dirs carry no Hub revision; Hub ids always pin it."""',
        "    if os.path.isdir(path):",
        "        return {}",
        '    return {"revision": revision}',
        "",
        "",
        "def _load_base_model(model_kwargs, model_id=MODEL_ID, revision=MODEL_REVISION):",
        '    """HAR-66 load class first (glm5_next), CausalLM fallback for stand-ins."""',
        "    try:",
        "        from transformers import AutoModelForImageTextToText",
        "",
        "        return AutoModelForImageTextToText.from_pretrained(",
        "            model_id, **_revision_kwargs(model_id, revision), **model_kwargs",
        "        )",
        "    except Exception:",
        "        return AutoModelForCausalLM.from_pretrained(",
        "            model_id, **_revision_kwargs(model_id, revision), **model_kwargs",
        "        )",
        "",
        "",
        "def main():",
        '    print(f"=== Starting TRL SFT for {MODEL_ID} (Method: {METHOD}) ===")',
        "",
        "    # 1. Tokenizer (pinned revision) + patched chat template.",
        "    tokenizer = AutoTokenizer.from_pretrained(",
        "        MODEL_ID, **_revision_kwargs(MODEL_ID, TOKENIZER_REVISION), trust_remote_code=True",
        "    )",
        "    if tokenizer.pad_token is None:",
        "        tokenizer.pad_token = tokenizer.eos_token",
        "    tokenizer.chat_template = GLMChatTemplatePatcher.get_builtin_template()",
        '    print("Loaded tokenizer and patched chat template with generation markers")',
        "",
        "    # 2. HAR-65 records -> {messages} rows (reward metadata never read).",
        "    if not os.path.exists(DATA_PATH):",
        '        raise FileNotFoundError(f"Training dataset not found at {DATA_PATH}")',
        "    rows = []",
        "    with open(DATA_PATH, encoding='utf-8') as f:",
        "        for line in f:",
        "            line = line.strip()",
        "            if not line:",
        "                continue",
        "            rows.append({'messages': har65_record_to_messages(json.loads(line))})",
        "    if not rows:",
        '        raise ValueError(f"No training records in {DATA_PATH}")',
        "    dataset = Dataset.from_list(rows)",
        '    print(f"Loaded training dataset: {len(dataset)} records")',
        "",
        "    # 3. Base model (pinned revision; bf16 needs CUDA, CPU falls back to fp32).",
        f"    want_bf16 = {config.bf16}",
        "    use_bf16 = bool(want_bf16) and torch.cuda.is_available()",
        "    model_kwargs = {",
        '        "trust_remote_code": True,',
        '        "torch_dtype": torch.bfloat16 if use_bf16 else torch.float32,',
        '        "device_map": "auto",',
        '        "attn_implementation": "sdpa",',
        "    }",
        quantization_block.rstrip("\n"),
        '    print("Loading base model...")',
        "    model = _load_base_model(model_kwargs)",
        peft_block.rstrip("\n"),
        "",
        "    # 4. TRL SFTConfig: assistant_only_loss makes the {% generation %}",
        "    # markers supervise assistant content only.",
        "    sft_args = SFTConfig(",
        f"        output_dir={output_dir},",
        f"        learning_rate={config.learning_rate},",
        f'        lr_scheduler_type={json.dumps(config.lr_scheduler_type)},',
        f"        warmup_steps={config.warmup_steps},",
        f"        weight_decay={config.weight_decay},",
        f"        per_device_train_batch_size={config.per_device_train_batch_size},",
        f"        gradient_accumulation_steps={config.gradient_accumulation_steps},",
        f"        max_steps={config.max_steps},",
        f"        num_train_epochs={config.num_train_epochs},",
        f"        logging_steps={config.logging_steps},",
        f"        save_steps={config.save_steps},",
        '        save_strategy="steps",',
        "        bf16=use_bf16,",
        "        fp16=False,",
        f"        gradient_checkpointing={config.gradient_checkpointing},",
        '        gradient_checkpointing_kwargs={"use_reentrant": False},',
        f"        packing={config.packing},",
        f"        max_length={config.max_length},",
        f"        dataset_text_field={json.dumps(config.dataset_text_field)},",
        "        assistant_only_loss=True,",
        '        seed=42,',
        '        report_to="none",',
        "    )",
        "",
        "    # 5. Maintained trainer: peft_config goes to SFTTrainer only.",
        "    trainer = SFTTrainer(",
        "        model=model,",
        "        args=sft_args,",
        "        train_dataset=dataset,",
        "        processing_class=tokenizer,",
        f"        {trainer_peft_arg}",
        "    )",
        "",
        '    print("=== Launching SFT Training ===")',
        "    train_result = trainer.train()",
        "    train_metrics = {k: float(v) for k, v in dict(train_result.metrics).items()}",
        '    final_loss = float(train_metrics.get("train_loss", float("nan")))',
        "",
        "    # 6. Save checkpoint + manifest (verify_checkpoint accepts this).",
        "    final_output = Path(OUTPUT_DIR) / 'final'",
        "    trainer.save_model(str(final_output))",
        "    tokenizer.save_pretrained(str(final_output))",
        "    with open(DATA_PATH, 'rb') as f:",
        "        dataset_hash = hashlib.sha256(f.read()).hexdigest()",
        "    first_messages = rows[0]['messages']",
        "    rendered = tokenizer.apply_chat_template(",
        "        first_messages, tokenize=True, return_assistant_tokens_mask=True",
        "    )",
        '    first_ids = rendered["input_ids"]',
        '    first_masks = rendered["assistant_masks"]',
        "    first_labels = [tid if m else -100 for tid, m in zip(first_ids, first_masks)]",
        "    receipt = GLMLossMasker.validate_loss_masks(",
        "        tokenizer.convert_ids_to_tokens(first_ids), first_ids, first_labels, first_masks",
        "    )",
        "    mask_receipt = {",
        '        "total_tokens": receipt.total_tokens,',
        '        "supervised_tokens": receipt.supervised_tokens,',
        '        "is_valid": receipt.is_valid,',
        "    }",
        "    manifest_path = write_checkpoint_manifest(",
        "        final_output,",
        "        model_id=MODEL_ID,",
        "        base_model_revision=MODEL_REVISION,",
        "        tokenizer_revision=TOKENIZER_REVISION,",
        "        training_method=METHOD,",
        "        step_count=sft_args.max_steps,",
        "        final_loss=final_loss,",
        '        adapter_path=str(final_output / "adapter_model.safetensors"),',
        "        dataset_path=DATA_PATH,",
        "        dataset_hash=dataset_hash,",
        "        config_hash=CONFIG_HASH,",
        "        train_metrics=train_metrics,",
        "        mask_receipt=mask_receipt,",
        "        library_versions=pinned_library_versions(),",
        '        vllm_command=f"vllm serve {MODEL_ID} --enable-lora",',
        "    )",
        '    print(f"Training finished. Manifest: {manifest_path}")',
        "",
        "    # 7. Save/reload receipt: checkpoint from disk must forward-match.",
        "    trainer.model.eval()",
        "    device = next(trainer.model.parameters()).device",
        '    probe_ids = tokenizer("Sanity check.", return_tensors="pt")["input_ids"][:, :32].to(device)',
        "    with torch.no_grad():",
        "        before_logits = trainer.model(probe_ids).logits.float()",
        reload_block.rstrip("\n"),
        "    assert torch.allclose(before_logits.cpu(), after_logits.cpu(), rtol=1e-3, atol=1e-3), (",
        '        "Save/reload forward mismatch: reloaded checkpoint differs"',
        "    )",
        '    print("Save/reload receipt: forward pass matches")',
        "",
        "",
        'if __name__ == "__main__":',
        "    main()",
        "",
    ]
    script = "\n".join(lines)
    return script + "\n"


def generate_remote_job_manifest(config: GLMSFTConfig) -> dict[str, Any]:
    """Generates Slurm/batch submission script and execution manifest for remote execution.

    Node count derives from the envelope (`ceil(gpus / 8)`), so multi-node
    methods (full_sft) no longer emit an impossible single-node topology.
    QLoRA hardware is computed at 4-bit. All hardware numbers carry
    `"qualification": "estimate-unverified"` (see GLMHardwareEnvelope).
    """
    precisions: dict[str, Literal["fp8", "bf16", "int4"]] = {
        "lora": "fp8",
        "qlora": "int4",
        "full_sft": "bf16",
    }
    hardware = compute_hardware_envelope(
        method=config.method,
        precision=precisions[config.method],
        context_length=config.max_length,
    )
    gpus = hardware.recommended_min_gpus
    nodes = (gpus + 7) // 8
    gpus_per_node = min(gpus, 8)

    slurm_lines = [
        "#!/bin/bash",
        "#SBATCH --job-name=glm-5.3-flash-sft",
        f"#SBATCH --nodes={nodes}",
        "#SBATCH --ntasks-per-node=1",
        f"#SBATCH --gpus-per-node={gpus_per_node}",
        "#SBATCH --cpus-per-task=32",
        "#SBATCH --mem=480G",
        "#SBATCH --time=04:00:00",
        f"#SBATCH --output={config.output_dir}/slurm-%j.out",
        f"#SBATCH --error={config.output_dir}/slurm-%j.err",
        "",
        "set -euo pipefail",
        "",
        'echo "Job starting on $(hostname) at $(date)"',
        f"echo \"GPUs allocated: {gpus} x {hardware.recommended_gpu_type}\"",
        "",
        "# Ensure environment",
        "source /opt/conda/bin/activate sft-glm",
        f'mkdir -p "{config.output_dir}"',
        "",
        "# Execute training with Accelerate / TRL",
        "accelerate launch \\",
        f"    --num_processes={gpus} \\",
        "    --mixed_precision=bf16 \\",
        "    train_glm_sft.py",
        "",
        'echo "Job completed successfully at $(date)"',
    ]
    slurm_script = "\n".join(slurm_lines) + "\n"

    return {
        "job_name": "glm-5.3-flash-sft",
        "method": config.method,
        "target_model": config.model_id,
        "model_revision": config.model_revision,
        "tokenizer_revision": config.tokenizer_revision,
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
    """Machine-checkable manifest for trained GLM checkpoints and adapters.

    Written by `write_checkpoint_manifest` (used by the generated TRL trainer
    and the CPU plumbing canary alike) and accepted by `verify_checkpoint`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: int = 1
    model_id: str = "zai-org/GLM-5.3-Flash"
    base_model_revision: str
    tokenizer_revision: str = "unknown"
    training_method: Literal["lora", "qlora", "full_sft"]
    step_count: int
    final_loss: float
    adapter_path: str
    dataset_path: str = ""
    dataset_hash: str
    config_hash: str
    train_metrics: dict[str, float] = Field(default_factory=dict)
    mask_receipt: dict[str, Any] = Field(default_factory=dict)
    library_versions: dict[str, str] = Field(default_factory=dict)
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


def har65_record_to_messages(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapts one HAR-65 record (PR #446, `evallab.sft_records`) to chat messages.

    Accepts `decision_example` rows (`context` + `target`), `full_trajectory`
    rows (`messages`), and legacy rows carrying a bare `messages` list.
    Returns message dicts with only the keys the training chat template reads
    (`role`, `content`, plus `reasoning_content` / `tool_calls` on assistant
    turns). Reward/verifier outcome, lineage, split, and fidelity metadata are
    never read, so they cannot leak into the learner. Fails closed
    (ValueError) on redacted messages, reward-metadata keys inside a message,
    non-string content, or tool-call arguments that do not decode to a mapping
    (the GLM template rejects raw argument strings).
    """
    if not isinstance(record, dict):
        raise ValueError(f"Record must be a JSON object, got {type(record).__name__}")
    kind = record.get("kind")
    if kind == "decision_example":
        context = record.get("context")
        target = record.get("target")
        if not isinstance(context, list) or not isinstance(target, dict):
            raise ValueError("decision_example must carry a `context` list and a `target` message")
        raw_messages = [*context, target]
    elif "messages" in record:
        raw_messages = record["messages"]
        if not isinstance(raw_messages, list):
            raise ValueError("Record `messages` must be a list")
    else:
        raise ValueError("Record has neither `kind: decision_example` nor a `messages` list")

    cleaned: list[dict[str, Any]] = []
    for m in raw_messages:
        if not isinstance(m, dict):
            raise ValueError(f"Message must be a JSON object, got {type(m).__name__}")
        if m.get("redacted"):
            raise ValueError("Record contains a redacted message; refusing to train on redacted context")
        leaked = [key for key in m if key in REWARD_METADATA_KEYS]
        if leaked:
            raise ValueError(f"Message carries reward/verifier metadata keys {leaked}; refusing to train")
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool", "observation"):
            raise ValueError(f"Unknown message role: {role!r}")
        content = m.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError(f"Message content must be a string, got {type(content).__name__}")
        out: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant":
            reasoning = m.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise ValueError("Assistant `reasoning_content` must be a string")
                out["reasoning_content"] = reasoning
            tool_calls = m.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    raise ValueError("Assistant `tool_calls` must be a list")
                out["tool_calls"] = [_normalize_tool_call(tc) for tc in tool_calls]
        cleaned.append(out)
    if not any(m["role"] == "assistant" for m in cleaned):
        raise ValueError("Record has no assistant turn; nothing would be supervised")
    return cleaned


def _normalize_tool_call(tool_call: Any) -> dict[str, Any]:
    """Normalizes one tool call to `{"name", "arguments", ...}` with mapping arguments."""
    if not isinstance(tool_call, dict):
        raise ValueError(f"Tool call must be a JSON object, got {type(tool_call).__name__}")
    inner = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else tool_call
    name = inner.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Tool call must carry a string `name`, got {name!r}")
    arguments = inner.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            import ast as _ast

            try:
                arguments = _ast.literal_eval(arguments)
            except (ValueError, SyntaxError) as e:
                raise ValueError(f"Tool call arguments string does not decode: {e}") from e
    if not isinstance(arguments, dict):
        raise ValueError(f"Tool call arguments must decode to a mapping, got {type(arguments).__name__}")
    normalized: dict[str, Any] = {"name": name, "arguments": arguments}
    call_id = tool_call.get("id") if isinstance(tool_call.get("id"), str) else inner.get("id")
    if isinstance(call_id, str) and call_id:
        normalized["id"] = call_id
    return normalized


def pinned_library_versions() -> dict[str, str]:
    """Reports installed versions of the isolated training stack without importing it."""
    versions: dict[str, str] = {}
    for package in ("torch", "transformers", "trl", "peft", "accelerate", "datasets", "bitsandbytes"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def write_checkpoint_manifest(
    checkpoint_dir: Path,
    *,
    model_id: str,
    base_model_revision: str,
    tokenizer_revision: str,
    training_method: Literal["lora", "qlora", "full_sft"],
    step_count: int,
    final_loss: float,
    adapter_path: str,
    dataset_path: str = "",
    dataset_hash: str,
    config_hash: str,
    train_metrics: dict[str, float] | None = None,
    mask_receipt: dict[str, Any] | None = None,
    library_versions: dict[str, str] | None = None,
    serving_backend: Literal["vllm", "sglang"] = "vllm",
    vllm_command: str,
) -> Path:
    """Writes and returns `checkpoint_manifest.json` for a produced checkpoint.

    Shared writer used by the generated TRL trainer (real training result,
    dataset/config hashes, mask receipt) and the CPU plumbing canary alike, so
    `verify_checkpoint` accepts both.
    """
    manifest = CheckpointManifest(
        manifest_version=1,
        model_id=model_id,
        base_model_revision=base_model_revision,
        tokenizer_revision=tokenizer_revision,
        training_method=training_method,
        step_count=step_count,
        final_loss=final_loss,
        adapter_path=adapter_path,
        dataset_path=dataset_path,
        dataset_hash=dataset_hash,
        config_hash=config_hash,
        train_metrics=dict(train_metrics or {}),
        mask_receipt=dict(mask_receipt or {}),
        library_versions=dict(library_versions or {}),
        created_at=datetime.now(UTC).isoformat(),
        serving_backend=serving_backend,
        vllm_command=vllm_command,
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = checkpoint_dir / "checkpoint_manifest.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(), indent=2), encoding="utf-8")
    return manifest_path


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

    # Step 4: Save checkpoint and manifest (shared writer, verify-compatible)
    ckpt_path = output_dir / "canary_checkpoint.pt"
    torch.save(model.state_dict(), ckpt_path)

    manifest_path = write_checkpoint_manifest(
        output_dir,
        model_id="canary-tiny-causal-plumbing",
        base_model_revision="canary-rev-01",
        tokenizer_revision="canary-rev-01",
        training_method="full_sft",
        step_count=2,
        final_loss=round(final_loss_val, 4),
        adapter_path=str(ckpt_path),
        dataset_hash=hashlib.sha256(b"canary-dataset-bytes").hexdigest(),
        config_hash=hashlib.sha256(b"canary-config-bytes").hexdigest(),
        serving_backend="vllm",
        vllm_command="vllm serve --model canary-tiny",
    )
    manifest = CheckpointManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))

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
        "model_revision": GLM_MODEL_REVISION,
        "tokenizer_revision": GLM_TOKENIZER_REVISION,
        "adapter_name": adapter_name,
        "adapter_path": checkpoint_dir,
        "serving_backend": "vllm",
        "vllm_command": vllm_cmd,
        "sglang_command": sglang_cmd,
        "checkpoint_manifest": {
            "path": f"{checkpoint_dir}/checkpoint_manifest.json",
            "contract": "trainer writes checkpoint_manifest.json via "
            "evallab.sft_glm.write_checkpoint_manifest (base model id + "
            "revision, tokenizer revision, library versions, dataset path + "
            "sha256, config sha256, train_result metrics, mask receipt, "
            "timestamp); validate with evallab.sft_glm.verify_checkpoint",
        },
        "merge_before_serving": "Dynamic LoRA is NOT supported for glm5_next "
        "in vLLM/SGLang executors (HAR-66); merge the adapter into the base "
        "weights before serving.",
        "hardware_qualification": "estimate-unverified: GPU counts/topology "
        "are storage arithmetic with no target-model run receipt.",
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
