"""Contract tests for GLM-5.3-Flash SFT training and serving runtime."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.sft_glm import (
    GLM_5_3_FLASH_SPECS,
    GLM_MODEL_REVISION,
    CheckpointManifest,
    GLMChatTemplatePatcher,
    GLMLossMasker,
    GLMSFTConfig,
    GLMTrainingRecord,
    compute_hardware_envelope,
    generate_re_serving_handoff,
    generate_remote_job_manifest,
    generate_trl_training_script,
    har65_record_to_messages,
    run_cpu_canary_smoke,
    verify_checkpoint,
    write_checkpoint_manifest,
)

GLM_SNAPSHOT = (
    Path.home()
    / ".cache/huggingface/hub/models--zai-org--GLM-5.3-Flash/snapshots"
    / GLM_MODEL_REVISION
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
    assert env.qualification == "estimate-unverified"


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
    assert env.qualification == "estimate-unverified"


def test_compute_hardware_envelope_qlora() -> None:
    """Verifies memory arithmetic for 4-bit quantized base LoRA."""
    env = compute_hardware_envelope(method="qlora", precision="int4", context_length=16384)
    assert env.method == "qlora"
    assert env.precision == "int4"
    assert env.weights_memory_gb >= 140.0  # ~160 GB in 4-bit
    assert env.is_single_node_feasible is True
    assert env.recommended_min_gpus == 4
    assert env.qualification == "estimate-unverified"


def _minimal_upstream_shaped_template() -> str:
    return (
        "[gMASK]<sop>\n"
        "{%- for m in messages -%}\n"
        "{%- if m.role == 'user' -%}<|user|>{{ m.content }}\n"
        "{%- elif m.role == 'assistant' -%}\n"
        "<|assistant|>{{ m.content }}{% endif %}\n"
        "{%- elif m.role == 'tool' -%}<|observation|>{{ m.content }}\n"
        "{%- endif -%}\n"
        "{%- endfor -%}\n"
        "{%- if add_generation_prompt -%}\n"
        "    <|assistant|><think>\n"
        "{%- endif -%}"
    )


def test_chat_template_patcher_upstream_anchors() -> None:
    """Patches a template with the real upstream anchors into one balanced pair."""
    patched = GLMChatTemplatePatcher.patch_template(_minimal_upstream_shaped_template())
    assert patched.count("{% generation %}") == 1
    assert patched.count("{% endgeneration %}") == 1
    assert patched.index("{% generation %}") < patched.index("{% endgeneration %}")
    tail = patched.split("{%- if add_generation_prompt -%}")[1]
    assert "{% generation %}" not in tail and "{% endgeneration %}" not in tail

    builtin = GLMChatTemplatePatcher.get_builtin_template()
    assert GLMChatTemplatePatcher.patch_template(builtin) == builtin

    with pytest.raises(ValueError):
        GLMChatTemplatePatcher.patch_template("no assistant marker here")
    with pytest.raises(ValueError):
        # Assistant marker but not the upstream branch anchors: fail closed.
        GLMChatTemplatePatcher.patch_template("<|assistant|> bare")


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
    assert receipt.preamble_supervised is False


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


def test_loss_masking_rejects_preamble_supervision() -> None:
    """Supervised tokens before the first role delimiter fail validation."""
    tokens = ["[gMASK]", "<sop>", "<|user|>", "hi", "<|assistant|>", "yo"]
    input_ids = [10, 11, 12, 13, 14, 15]
    labels = [10, -100, -100, -100, -100, 15]
    loss_mask = [1, 0, 0, 0, 0, 1]

    receipt = GLMLossMasker.validate_loss_masks(tokens, input_ids, labels, loss_mask)
    assert receipt.preamble_supervised is True
    assert receipt.is_valid is False


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


def test_training_record_rejects_unpreceded_tool_anywhere() -> None:
    """A tool/observation with no preceding assistant turn fails at any position."""
    record = GLMTrainingRecord(
        record_id="rec-03",
        task_id="task-01",
        source_split="train",
        messages=[
            {"role": "user", "content": "Fix code"},
            {"role": "user", "content": "More context"},
            {"role": "observation", "content": "Orphan result"},
        ],
    )
    assert record.validate_causal_ordering() is False


def test_training_record_rejects_reward_metadata() -> None:
    """Reward/verifier keys in any message or record metadata fail validation."""
    in_message = GLMTrainingRecord(
        record_id="rec-04",
        task_id="task-01",
        source_split="train",
        messages=[
            {"role": "user", "content": "Fix code"},
            {"role": "assistant", "content": "Done", "verified_success": True},
        ],
    )
    assert in_message.validate_causal_ordering() is False

    in_metadata = GLMTrainingRecord(
        record_id="rec-05",
        task_id="task-01",
        source_split="train",
        messages=[{"role": "user", "content": "Fix code"}],
        metadata={"reward": 1.0},
    )
    assert in_metadata.validate_causal_ordering() is False


def test_har65_record_adapter() -> None:
    """HAR-65 rows map to template messages; leak metadata never reaches text."""
    decision = {
        "kind": "decision_example",
        "context": [{"role": "user", "content": "List files.", "step_id": 1}],
        "target": {
            "role": "assistant",
            "content": "I will list files.",
            "reasoning_content": "Need a listing.",
            "tool_calls": [{"id": "c1", "name": "bash", "arguments": '{"command": "ls"}'}],
            "step_id": 2,
        },
        "outcome": {"reward": 1.0, "failure_class": "passed"},
        "lineage": {"task_name": "tb4/x"},
    }
    messages = har65_record_to_messages(decision)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["tool_calls"] == [
        {"name": "bash", "arguments": {"command": "ls"}, "id": "c1"}
    ]
    assert all(set(m) <= {"role", "content", "reasoning_content", "tool_calls"} for m in messages)

    full = {
        "kind": "full_trajectory",
        "record_id": "r1",
        "messages": [
            {"role": "user", "content": "hi", "step_id": 0},
            {"role": "assistant", "content": "yo", "step_id": 1},
        ],
    }
    assert [m["role"] for m in har65_record_to_messages(full)] == ["user", "assistant"]

    with pytest.raises(ValueError):
        har65_record_to_messages(
            {
                "kind": "full_trajectory",
                "messages": [{"role": "user", "content": "hi", "redacted": True}],
            }
        )
    with pytest.raises(ValueError):
        har65_record_to_messages(
            {
                "kind": "full_trajectory",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "yo", "grade": "A"},
                ],
            }
        )
    with pytest.raises(ValueError):
        har65_record_to_messages(
            {"kind": "full_trajectory", "messages": [{"role": "user", "content": "hi"}]}
        )


def test_remote_job_manifest_generation() -> None:
    """Slurm nodes derive from the envelope; qlora is sized at 4-bit."""
    lora = generate_remote_job_manifest(GLMSFTConfig(output_dir="/tmp/sft-runs"))
    assert lora["job_name"] == "glm-5.3-flash-sft"
    assert lora["target_model"] == "zai-org/GLM-5.3-Flash"
    assert lora["model_revision"] == GLM_MODEL_REVISION
    assert "#SBATCH --nodes=1" in lora["slurm_submission_script"]
    assert "#SBATCH --gpus-per-node=8" in lora["slurm_submission_script"]
    assert "accelerate launch" in lora["slurm_submission_script"]
    assert lora["hardware_requirements"]["qualification"] == "estimate-unverified"

    full = generate_remote_job_manifest(GLMSFTConfig(method="full_sft", output_dir="/tmp/sft-runs"))
    assert "#SBATCH --nodes=9" in full["slurm_submission_script"]
    assert "#SBATCH --gpus-per-node=8" in full["slurm_submission_script"]

    qlora = generate_remote_job_manifest(GLMSFTConfig(method="qlora", output_dir="/tmp/sft-runs"))
    assert qlora["hardware_requirements"]["precision"] == "int4"
    assert "#SBATCH --nodes=1" in qlora["slurm_submission_script"]
    assert "#SBATCH --gpus-per-node=4" in qlora["slurm_submission_script"]


def test_checkpoint_manifest_and_verification(tmp_path: Path) -> None:
    """The shared writer's output is accepted by verify_checkpoint."""
    ckpt_dir = tmp_path / "checkpoint-01"
    ckpt_dir.mkdir()

    # Create dummy adapter files
    (ckpt_dir / "adapter_config.json").write_text("{}", encoding="utf-8")
    (ckpt_dir / "adapter_model.safetensors").write_bytes(b"dummy-weights")

    manifest_path = write_checkpoint_manifest(
        ckpt_dir,
        model_id="zai-org/GLM-5.3-Flash",
        base_model_revision="main",
        tokenizer_revision="main",
        training_method="lora",
        step_count=250,
        final_loss=0.824,
        adapter_path=str(ckpt_dir / "adapter_model.safetensors"),
        dataset_path="research/experiments/sft-glm-flash/dataset.jsonl",
        dataset_hash="abc123hash",
        config_hash="def456hash",
        train_metrics={"train_loss": 0.824},
        mask_receipt={"supervised_tokens": 128, "is_valid": True},
        library_versions={"trl": "1.13.0"},
        serving_backend="vllm",
        vllm_command="vllm serve ...",
    )
    assert manifest_path == ckpt_dir / "checkpoint_manifest.json"

    res = verify_checkpoint(ckpt_dir)
    assert res["valid"] is True
    assert res["manifest"]["final_loss"] == 0.824
    assert res["manifest"]["mask_receipt"]["supervised_tokens"] == 128


def test_re_serving_handoff() -> None:
    """Verifies RE serving adoption handoff specification."""
    handoff = generate_re_serving_handoff(
        checkpoint_dir="/models/glm-flash-adapter",
        adapter_name="glm-flash-sft-v1",
    )
    assert handoff["target_model"] == "zai-org/GLM-5.3-Flash"
    assert handoff["model_selector"] == "zai/glm-5.3-flash"
    assert handoff["model_revision"] == GLM_MODEL_REVISION
    assert handoff["tokenizer_revision"] == GLM_MODEL_REVISION
    assert "--enable-lora" in handoff["vllm_command"]
    assert "--lora-modules glm-flash-sft-v1=/models/glm-flash-adapter" in handoff["vllm_command"]
    assert "--tensor-parallel-size 8" in handoff["vllm_command"]
    assert handoff["pinned_inference_settings"]["reasoning_effort"] == "max"
    assert handoff["pinned_inference_settings"]["clear_thinking"] is False
    assert handoff["evaluation_protocol"]["harness"] == "mini-swe-agent (stock, fixed)"
    assert handoff["evaluation_protocol"]["benchmark"] == "TB4 (Terminal-Bench 4)"
    assert "checkpoint_manifest.json" in handoff["checkpoint_manifest"]["contract"]
    assert "estimate-unverified" in handoff["hardware_qualification"]


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


# ---------------------------------------------------------------------------
# Isolated-environment proofs (skipped in core CI via importorskip).
# Run: PYTHONPATH=src HF_HUB_OFFLINE=1 /tmp/test-torch/bin/python \
#        -m pytest tests/test_sft_glm.py -q -p no:xdist
# ---------------------------------------------------------------------------


def _require_snapshot() -> Path:
    if not (GLM_SNAPSHOT / "chat_template.jinja").exists():
        pytest.skip(f"GLM snapshot {GLM_MODEL_REVISION} not in HF cache")
    return GLM_SNAPSHOT


def test_patched_upstream_template_masks_assistant_only() -> None:
    """Proves the patcher on the real upstream template with the real tokenizer.

    Loads chat_template.jinja from the pinned HF cache snapshot, patches it,
    renders with return_assistant_tokens_mask=True, and asserts only
    assistant-content tokens are supervised (via GLMLossMasker). Also asserts
    the patched upstream template renders byte-identical text and masks to the
    builtin template for tool-role conversations.
    """
    pytest.importorskip("transformers")
    snapshot = _require_snapshot()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        "zai-org/GLM-5.3-Flash", revision=GLM_MODEL_REVISION, trust_remote_code=True
    )
    upstream = (snapshot / "chat_template.jinja").read_text(encoding="utf-8")
    assert upstream.count("{% generation %}") == 0  # stock template has no markers
    patched = GLMChatTemplatePatcher.patch_template(upstream)

    messages = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "List files."},
        {
            "role": "assistant",
            "reasoning_content": "Need a listing first.",
            "content": "I will list files.",
            "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}],
        },
        {"role": "tool", "content": "a.py b.py"},
        {"role": "assistant", "reasoning_content": "Done.", "content": "Listed."},
    ]
    tokenizer.chat_template = patched
    from_patched = tokenizer.apply_chat_template(
        messages, tokenize=True, return_assistant_tokens_mask=True
    )
    tokenizer.chat_template = GLMChatTemplatePatcher.get_builtin_template()
    from_builtin = tokenizer.apply_chat_template(
        messages, tokenize=True, return_assistant_tokens_mask=True
    )
    assert tokenizer.decode(from_patched["input_ids"]) == tokenizer.decode(
        from_builtin["input_ids"]
    )
    assert from_patched["assistant_masks"] == from_builtin["assistant_masks"]

    input_ids = from_patched["input_ids"]
    masks = from_patched["assistant_masks"]
    labels = [tid if m else -100 for tid, m in zip(input_ids, masks)]
    receipt = GLMLossMasker.validate_loss_masks(
        tokenizer.convert_ids_to_tokens(input_ids), input_ids, labels, masks
    )
    assert receipt.is_valid is True
    assert receipt.supervised_tokens > 0
    assert receipt.system_supervised is False
    assert receipt.user_supervised is False
    assert receipt.observation_supervised is False
    assert receipt.preamble_supervised is False


def _write_canary_dataset(path: Path) -> None:
    records = []
    for i in range(4):
        tool_call: dict = {"id": f"c{i}", "name": "bash", "arguments": {"command": "ls"}}
        if i == 2:
            # HAR-66 finding 6: arguments may arrive as a JSON string.
            tool_call = {"id": "c2", "name": "bash", "arguments": '{"command": "ls -la"}'}
        records.append(
            {
                "kind": "full_trajectory",
                "record_id": f"canary-{i}",
                "messages": [
                    {"role": "system", "content": "You are a coding agent.", "step_id": 0},
                    {"role": "user", "content": f"Task {i}: list files.", "step_id": 1},
                    {
                        "role": "assistant",
                        "content": "I will list files.",
                        "reasoning_content": "Need a listing first.",
                        "tool_calls": [tool_call],
                        "step_id": 2,
                    },
                    {
                        "role": "observation",
                        "content": "a.py b.py",
                        "presented_as": "tool",
                        "step_id": 3,
                    },
                    {
                        "role": "assistant",
                        "content": "Listed the files.",
                        "reasoning_content": "Done.",
                        "tool_calls": [],
                        "step_id": 4,
                    },
                ],
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_generated_trainer_trains_writes_manifest_and_verifies(tmp_path: Path) -> None:
    """Executes the generated script: tiny stand-in model, real GLM tokenizer.

    Plumbing canary only: a random-init Llama (vocab sized to the real GLM
    tokenizer) proves the emitted SFTConfig/SFTTrainer construct and train
    under trl 1.13.0, write checkpoint_manifest.json, reload, and pass
    verify_checkpoint. It is NOT a GLM-5.3-Flash result.
    """
    pytest.importorskip("trl")
    pytest.importorskip("transformers")
    snapshot = _require_snapshot()
    from transformers import LlamaConfig, LlamaForCausalLM

    # Emission shape for all three methods (textual; only lora executes here).
    lora_script = generate_trl_training_script(GLMSFTConfig())
    assert "assistant_only_loss=True" in lora_script
    assert "max_seq_length" not in lora_script
    assert "warmup_ratio" not in lora_script
    assert "get_peft_model(" not in lora_script
    assert "revision=" in lora_script
    qlora_script = generate_trl_training_script(GLMSFTConfig(method="qlora"))
    assert "BitsAndBytesConfig" in qlora_script and "load_in_4bit=True" in qlora_script
    assert "peft_config=peft_config" in qlora_script
    full_script = generate_trl_training_script(GLMSFTConfig(method="full_sft"))
    assert "peft_config=None" in full_script
    assert "LoraConfig" not in full_script

    tiny_dir = tmp_path / "tiny"
    tiny_dir.mkdir()
    LlamaForCausalLM(
        LlamaConfig(
            vocab_size=154856,
            hidden_size=32,
            num_hidden_layers=2,
            num_attention_heads=4,
            num_key_value_heads=4,
            intermediate_size=64,
        )
    ).save_pretrained(tiny_dir)
    for name in ("tokenizer.json", "tokenizer_config.json"):
        shutil.copy(snapshot / name, tiny_dir / name)

    dataset_path = tmp_path / "dataset.jsonl"
    _write_canary_dataset(dataset_path)
    output_dir = tmp_path / "out"
    config = GLMSFTConfig(
        model_id=str(tiny_dir),
        dataset_path=str(dataset_path),
        output_dir=str(output_dir),
        max_steps=2,
    )
    script_path = tmp_path / "train_glm_sft.py"
    script_path.write_text(generate_trl_training_script(config), encoding="utf-8")

    repo_src = Path(__file__).resolve().parents[1] / "src"
    env = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "PYTHONPATH": str(repo_src),
    }
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, f"trainer failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"

    final_dir = output_dir / "final"
    manifest_path = final_dir / "checkpoint_manifest.json"
    assert manifest_path.exists()
    res = verify_checkpoint(final_dir)
    assert res["valid"] is True, res.get("error")
    manifest = res["manifest"]
    assert manifest["base_model_revision"] == GLM_MODEL_REVISION
    assert manifest["tokenizer_revision"] == GLM_MODEL_REVISION
    assert manifest["dataset_path"] == str(dataset_path)
    assert manifest["mask_receipt"]["is_valid"] is True

    # The manifest's supervised-token count equals the masker's own count on
    # the same first record rendered through the same template.
    from transformers import AutoTokenizer

    from evallab.sft_glm import GLMChatTemplatePatcher as Patcher

    tokenizer = AutoTokenizer.from_pretrained(str(tiny_dir), trust_remote_code=True)
    tokenizer.chat_template = Patcher.get_builtin_template()
    first_record = json.loads(dataset_path.read_text(encoding="utf-8").splitlines()[0])
    first_messages = har65_record_to_messages(first_record)
    rendered = tokenizer.apply_chat_template(
        first_messages, tokenize=True, return_assistant_tokens_mask=True
    )
    assert manifest["mask_receipt"]["supervised_tokens"] == sum(rendered["assistant_masks"])
