"""Contract tests for GLM-5.3-Flash SFT training and serving runtime."""

import json
from pathlib import Path

import pytest

from evallab.sft_glm import (
    GLM_5_3_FLASH_SPECS,
    CheckpointManifest,
    GLMChatTemplatePatcher,
    GLMLossMasker,
    GLMSFTConfig,
    GLMTrainingRecord,
    compute_hardware_envelope,
    generate_re_serving_handoff,
    generate_remote_job_manifest,
    generate_trl_training_script,
    run_cpu_canary_smoke,
    verify_checkpoint,
)


def test_glm_model_specs() -> None:
    """Verifies that GLM-5.3-Flash specifications accurately reflect model architecture."""
    specs = GLM_5_3_FLASH_SPECS
    assert specs.model_id == "zai-org/GLM-5.3-Flash"
    assert specs.model_selector == "zai/glm-5.3-flash"
    assert specs.architecture == "Glm5NextForConditionalGeneration"
    assert specs.total_parameters == 320_000_000_000
    assert specs.active_parameters == 18_000_000_000
    assert specs.num_hidden_layers == 45
    assert specs.num_routed_experts == 288
    assert specs.num_shared_experts == 1
    assert specs.num_experts_per_tok == 8
    assert specs.vocab_size == 154880
    assert specs.pad_token_id == 154820
    assert 154820 in specs.eos_token_ids
    assert specs.max_position_embeddings == 1048576
    assert specs.linear_attention_layers == 34
    assert specs.sparse_attention_layers == 11
    assert "<|assistant|>" in specs.role_tokens


def test_compute_hardware_envelope_full_sft() -> None:
    """Verifies memory arithmetic and cluster requirements for full SFT."""
    env = compute_hardware_envelope(method="full_sft", precision="bf16", context_length=16384)
    assert env.method == "full_sft"
    assert env.precision == "bf16"
    assert env.weights_memory_gb >= 590.0  # ~640 GB
    assert env.optimizer_memory_gb >= 2000.0  # >2.3 TB optimizer states
    assert env.is_single_node_feasible is False
    assert env.recommended_min_gpus >= 72
    assert "H100" in env.recommended_gpu_type


def test_compute_hardware_envelope_lora() -> None:
    """Verifies memory arithmetic and single-node feasibility for LoRA."""
    env = compute_hardware_envelope(method="lora", precision="fp8", context_length=16384)
    assert env.method == "lora"
    assert env.precision == "fp8"
    assert env.weights_memory_gb >= 290.0  # ~320 GB in FP8
    assert env.optimizer_memory_gb < 10.0  # ~2.4 GB for LoRA adapter
    assert env.is_single_node_feasible is True
    assert env.recommended_min_gpus == 8
    assert env.total_vram_required_gb < 600.0  # Fits inside 8x 80GB = 640GB pool


def test_compute_hardware_envelope_qlora() -> None:
    """Verifies memory arithmetic for 4-bit quantized base LoRA."""
    env = compute_hardware_envelope(method="qlora", precision="int4", context_length=16384)
    assert env.method == "qlora"
    assert env.weights_memory_gb >= 140.0  # ~160 GB in 4-bit
    assert env.is_single_node_feasible is True
    assert env.recommended_min_gpus == 4


def test_chat_template_patcher() -> None:
    """Verifies generation tag injection into chat template."""
    raw_template = "{%- elif m.role == 'assistant' -%}\n<|assistant|>\ncontent\n{%- elif m.role == 'tool' -%}"
    patched = GLMChatTemplatePatcher.patch_template(raw_template)
    assert "{% generation %}" in patched
    assert "{% endgeneration %}" in patched

    builtin = GLMChatTemplatePatcher.get_builtin_template()
    assert "{% generation %}" in builtin
    assert "{% endgeneration %}" in builtin
    assert "<|assistant|>{% generation %}" in builtin


def test_loss_masking_contract_valid() -> None:
    """Verifies valid loss mask receipt when only assistant tokens are supervised."""
    tokens = [
        "<|system|>", "Sys", "prompt",
        "<|user|>", "Run", "tests",
        "<|assistant|>", "<think>", "Plan", "</think>", "Running", "now",
        "<|observation|>", "Pass",
    ]
    input_ids = list(range(len(tokens)))
    # Labels: -100 for non-assistant, exact id for assistant
    labels = [-100] * 7 + [7, 8, 9, 10, 11] + [-100, -100]
    loss_mask = [0] * 7 + [1, 1, 1, 1, 1] + [0, 0]

    receipt = GLMLossMasker.validate_loss_masks(tokens, input_ids, labels, loss_mask)
    assert receipt.is_valid is True
    assert receipt.supervised_tokens == 5
    assert receipt.unsupervised_tokens == 9
    assert receipt.has_empty_target is False
    assert receipt.system_supervised is False
    assert receipt.user_supervised is False
    assert receipt.observation_supervised is False
    assert receipt.delimiter_supervised is False


def test_loss_masking_contract_detects_leakage() -> None:
    """Verifies detection when non-assistant tokens leak into supervision."""
    tokens = ["<|user|>", "Command", "<|assistant|>", "Response"]
    input_ids = [1, 2, 3, 4]
    # Leaking user token into labels
    labels = [-100, 2, -100, 4]
    loss_mask = [0, 1, 0, 1]

    receipt = GLMLossMasker.validate_loss_masks(tokens, input_ids, labels, loss_mask)
    assert receipt.is_valid is False
    assert receipt.user_supervised is True


def test_training_record_causal_validation() -> None:
    """Verifies causal validation of demonstration records."""
    valid_record = GLMTrainingRecord(
        record_id="rec-01",
        task_id="task-01",
        source_split="train",
        messages=[
            {"role": "user", "content": "Fix code"},
            {"role": "assistant", "content": "I will fix it"},
            {"role": "tool", "content": "Tool output"},
            {"role": "assistant", "content": "Done"},
        ],
    )
    assert valid_record.validate_causal_ordering() is True

    # Invalid: tool observation at the very beginning with no prior command
    invalid_record = GLMTrainingRecord(
        record_id="rec-02",
        task_id="task-01",
        source_split="train",
        messages=[
            {"role": "tool", "content": "Floating observation"},
            {"role": "assistant", "content": "Action"},
        ],
    )
    assert invalid_record.validate_causal_ordering() is False


def test_trl_training_script_generation() -> None:
    """Verifies reproducible TRL training script generation."""
    config = GLMSFTConfig(output_dir="/tmp/test-sft-out", max_steps=100)
    script = generate_trl_training_script(config)
    assert "SFTTrainer" in script
    assert "SFTConfig" in script
    assert "LoraConfig" in script
    assert "zai-org/GLM-5.3-Flash" in script
    assert "GLMChatTemplatePatcher.get_builtin_template()" in script


def test_remote_job_manifest_generation() -> None:
    """Verifies Slurm submission script and job manifest generation."""
    config = GLMSFTConfig(output_dir="/tmp/sft-runs")
    manifest = generate_remote_job_manifest(config)
    assert manifest["job_name"] == "glm-5.3-flash-sft"
    assert manifest["target_model"] == "zai-org/GLM-5.3-Flash"
    assert "#SBATCH --gpus-per-node=8" in manifest["slurm_submission_script"]
    assert "accelerate launch" in manifest["slurm_submission_script"]


def test_checkpoint_manifest_and_verification(tmp_path: Path) -> None:
    """Verifies checkpoint manifest creation, serialization, and disk verification."""
    ckpt_dir = tmp_path / "checkpoint-01"
    ckpt_dir.mkdir()

    # Create dummy adapter files
    (ckpt_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (ckpt_dir / "adapter_model.safetensors").write_bytes(b"dummy-weights")

    manifest = CheckpointManifest(
        manifest_version=1,
        model_id="zai-org/GLM-5.3-Flash",
        base_model_revision="main",
        training_method="lora",
        step_count=250,
        final_loss=0.824,
        adapter_path=str(ckpt_dir / "adapter_model.safetensors"),
        dataset_hash="abc123hash",
        config_hash="def456hash",
        created_at="2026-09-18T12:00:00Z",
        serving_backend="vllm",
        vllm_command="vllm serve ...",
    )
    (ckpt_dir / "checkpoint_manifest.json").write_text(
        json.dumps(manifest.model_dump(), indent=2), encoding="utf-8"
    )

    res = verify_checkpoint(ckpt_dir)
    assert res["valid"] is True
    assert res["manifest"]["final_loss"] == 0.824


def test_re_serving_handoff() -> None:
    """Verifies RE serving adoption handoff specification."""
    handoff = generate_re_serving_handoff(
        checkpoint_dir="/models/glm-flash-adapter",
        adapter_name="glm-flash-sft-v1",
    )
    assert handoff["target_model"] == "zai-org/GLM-5.3-Flash"
    assert handoff["model_selector"] == "zai/glm-5.3-flash"
    assert "--enable-lora" in handoff["vllm_command"]
    assert "--lora-modules glm-flash-sft-v1=/models/glm-flash-adapter" in handoff["vllm_command"]
    assert "--tensor-parallel-size 8" in handoff["vllm_command"]
    assert handoff["pinned_inference_settings"]["reasoning_effort"] == "max"
    assert handoff["pinned_inference_settings"]["clear_thinking"] is False
    assert handoff["evaluation_protocol"]["harness"] == "mini-swe-agent (stock, fixed)"
    assert handoff["evaluation_protocol"]["benchmark"] == "TB4 (Terminal-Bench 4)"


def test_cpu_canary_smoke(tmp_path: Path) -> None:
    """Runs genuine backward pass, update, save and reload check on CPU."""
    pytest.importorskip("torch")
    canary_dir = tmp_path / "canary"
    result = run_cpu_canary_smoke(canary_dir)

    assert result["level"] == "cpu_plumbing_canary"
    assert result["is_target_model"] is False
    assert result["loss_decreased"] is True
    assert result["weight_norm_delta"] > 0.0
    assert result["reload_verified"] is True
    assert Path(result["checkpoint_path"]).exists()
    assert (canary_dir / "checkpoint_manifest.json").exists()
