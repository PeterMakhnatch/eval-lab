# /// script
# requires-python = ">=3.12"
# dependencies = ["transformers==5.16.0", "tokenizers==0.23.2", "jinja2==3.1.6", "pyyaml==6.0.3"]
# ///
"""HAR-66 public-metadata/tokenizer qualification; never loads model weights.

Run with ``uv run --no-project probe.py --assets-dir <cache> --download``.
Download is an explicit, hash-pinned allowlist of metadata and tokenizer assets.
The conversations below are protocol fixtures, not demonstrations or model runs.
The receipt reports known training gaps separately from failed probe assertions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import platform
import sys
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from transformers import AutoConfig, AutoTokenizer

MODEL_ID = "zai-org/GLM-5.3-Flash"
REVISION = "eb9eb208eb0d988989d07a6a12d0fdeb5f52574a"
ALLOWED_ASSETS = frozenset(
    {
        "config.json",
        "chat_template.jinja",
        "tokenizer_config.json",
        "generation_config.json",
        "tokenizer.json",
        "model.safetensors.index.json",
        "LICENSE",
    }
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in the task environment.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]
MESSAGES = [
    {"role": "system", "content": "PROTOCOL_FIXTURE_SYSTEM: not a training example."},
    {"role": "user", "content": "PROTOCOL_FIXTURE_USER: inspect the workspace."},
    {
        "role": "assistant",
        "content": "",
        "reasoning_content": "PRIOR_REASONING_SENTINEL: inspect before editing.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": {"command": "printf 'first'"}},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "PRIOR_OBSERVATION_SENTINEL"},
    {"role": "assistant", "content": "PROTOCOL_FIXTURE_PRIOR_FINAL"},
    {"role": "user", "content": "PROTOCOL_FIXTURE_NEW_USER: now run the check."},
    {
        "role": "assistant",
        "content": "",
        "reasoning_content": "CURRENT_REASONING_SENTINEL: verify the change.",
        "tool_calls": [
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "bash", "arguments": {"command": "printf 'second'"}},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_2", "content": "CURRENT_OBSERVATION_SENTINEL"},
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def acquire_assets(manifest: dict, directory: Path, *, download: bool) -> list[dict]:
    if manifest["model_id"] != MODEL_ID or manifest["revision"] != REVISION:
        raise ValueError("Probe only qualifies the selected exact model revision")
    specs = manifest["assets"]
    if {spec["name"] for spec in specs} != ALLOWED_ASSETS or len(specs) != len(ALLOWED_ASSETS):
        raise ValueError("Unexpected or missing asset; model weights are never allowed")
    directory.mkdir(parents=True, exist_ok=True)
    receipts = []
    for spec in specs:
        name = spec["name"]
        expected_url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}"
        if spec["url"] != expected_url or not 0 < spec["size_bytes"] <= 25_000_000:
            raise ValueError(f"Unpinned or oversized asset: {name}")
        destination = directory / name
        if destination.exists():
            if destination.is_symlink():
                raise ValueError(f"Asset must not be a symlink: {name}")
            data = destination.read_bytes()
            origin = "verified_local_cache"
        elif download:
            request = urllib.request.Request(expected_url, headers={"User-Agent": "eval-lab-har66/1"})
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read(spec["size_bytes"] + 1)
            origin = "public_metadata_download"
        else:
            raise FileNotFoundError(f"Missing {name}; use --download for metadata-only acquisition")
        if len(data) != spec["size_bytes"] or sha256(data) != spec["sha256"]:
            raise ValueError(f"Source digest/size mismatch: {name}")
        if origin == "public_metadata_download":
            destination.write_bytes(data)
        receipts.append({"name": name, "sha256": sha256(data), "size_bytes": len(data), "origin": origin})
    return receipts


def qualify(directory: Path) -> dict:
    config = json.loads((directory / "config.json").read_text())
    generation = json.loads((directory / "generation_config.json").read_text())
    index = json.loads((directory / "model.safetensors.index.json").read_text())
    template = (directory / "chat_template.jinja").read_text()
    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
    try:
        auto_config = AutoConfig.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        architecture_registration = {"recognized": auto_config.model_type == "glm5_next", "class": type(auto_config).__name__}
    except ValueError as exc:
        architecture_registration = {"recognized": False, "error_type": type(exc).__name__, "error": str(exc)}
    checks = []

    def check(name: str, condition: bool, observed: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "observed": observed})

    def render(messages: list[dict], **kwargs) -> str:
        return tokenizer.apply_chat_template(messages, tools=TOOLS, tokenize=False, **kwargs)

    text_config = config["text_config"]
    layers = dict(Counter(text_config["layer_types"]))
    check("exact_architecture_recognized", architecture_registration["recognized"], architecture_registration)
    check("decoder_layout", layers == {"linear_attention": 34, "deepseek_sparse_attention": 11}, layers)
    check("expert_layout", (text_config["n_routed_experts"], text_config["num_experts_per_tok"]) == (288, 8),
          {"routed": text_config["n_routed_experts"], "active_per_token": text_config["num_experts_per_tok"]})
    check("released_base_is_fp8", config["quantization_config"]["quant_method"] == "fp8",
          config["quantization_config"]["quant_method"])

    rendered = render(MESSAGES, add_generation_prompt=True)
    check("native_bash_xml_action", "<tool_call>bash<arg_key>command</arg_key><arg_value>printf 'second'</arg_value></tool_call>" in rendered,
          "Native tool-call arguments render as GLM XML, not JSON inside tool_call")
    check("observations_are_separate", "<|observation|><tool_response>CURRENT_OBSERVATION_SENTINEL</tool_response>" in rendered,
          "Observation is outside assistant response")
    check("generation_opens_thinking", rendered.endswith("<|assistant|><think>"), rendered[-60:])
    check("default_retains_prior_reasoning", "PRIOR_REASONING_SENTINEL" in rendered, "clear_thinking defaults false")
    cleared = render(MESSAGES, add_generation_prompt=True, clear_thinking=True)
    check("clear_thinking_is_turn_sensitive", "PRIOR_REASONING_SENTINEL" not in cleared and "CURRENT_REASONING_SENTINEL" in cleared,
          "Only reasoning before the latest user turn is removed")
    efforts = {}
    for effort, expected in [("low", "Low"), ("high", "High"), ("max", "Max"), ("xhigh", "Max")]:
        result = render(MESSAGES, add_generation_prompt=True, reasoning_effort=effort)
        efforts[effort] = expected
        check(f"effort_{effort}", f"<|system|>Reasoning Effort: {expected}" in result, expected)

    raw_arguments = copy.deepcopy(MESSAGES)
    for message in raw_arguments:
        for call in message.get("tool_calls", []):
            call["function"]["arguments"] = json.dumps(call["function"]["arguments"])
    try:
        raw_result = render(raw_arguments, add_generation_prompt=True)
        raw_arguments_result = {"accepted": True, "same_as_mapping": raw_result == rendered}
    except Exception as exc:
        raw_arguments_result = {"accepted": False, "error_type": type(exc).__name__, "error": str(exc)}
    check("raw_argument_string_boundary_observed", not raw_arguments_result["accepted"], raw_arguments_result)

    completed = MESSAGES + [{"role": "assistant", "content": "FINAL_ASSISTANT_SENTINEL"}]
    encoded = tokenizer.apply_chat_template(
        completed, tools=TOOLS, tokenize=True, add_generation_prompt=False,
        return_dict=True, return_assistant_tokens_mask=True,
    )
    mask = encoded.get("assistant_masks", encoded.get("assistant_tokens_mask", []))
    native_mask = {
        "mask_field_present": bool(mask),
        "sequence_tokens": len(encoded["input_ids"]),
        "supervised_tokens": sum(mask),
        "template_has_generation_tags": "{% generation" in template or "{%- generation" in template,
    }
    check("native_assistant_mask_gap_detected", sum(mask) == 0, native_mask)
    roundtrip = tokenizer.decode(encoded["input_ids"], skip_special_tokens=False)
    check("tokenizer_preserves_action_and_observation", "<tool_call>bash" in roundtrip and "CURRENT_OBSERVATION_SENTINEL" in roundtrip,
          {"tokens": len(encoded["input_ids"]), "render_sha256": sha256(rendered.encode())})
    eos_tokens = {str(token_id): tokenizer.convert_ids_to_tokens(token_id) for token_id in generation["eos_token_id"]}
    check("stop_tokens_resolved", eos_tokens == {"154820": "<|endoftext|>", "154827": "<|user|>", "154829": "<|observation|>"}, eos_tokens)
    final_rendered = render(completed, add_generation_prompt=False)
    final_ids = tokenizer(final_rendered, add_special_tokens=False)["input_ids"]
    stop_appended = bool(final_ids and final_ids[-1] in generation["eos_token_id"])
    check("final_stop_boundary_gap_detected", not stop_appended, {"last_token_id": final_ids[-1], "automatic_terminal_stop_appended": stop_appended})
    return {
        "checks": checks,
        "architecture": {
            "model_type": config["model_type"], "architectures": config["architectures"],
            "transformers_registration": architecture_registration,
            "configured_context_tokens": text_config["max_position_embeddings"],
            "training_context_qualified": False, "layers": layers,
            "checkpoint_tensor_bytes": index["metadata"]["total_size"],
            "checkpoint_tensor_gib": index["metadata"]["total_size"] / 2**30,
            "expert_checkpoint_sample": [name for name in index["weight_map"] if "layers.3.mlp.experts.0." in name],
        },
        "protocol": {"reasoning_effort_rendering": efforts, "stop_tokens": eos_tokens,
                     "raw_argument_strings": raw_arguments_result, "native_assistant_mask": native_mask},
        "consumer_requirements": [
            "Decode raw OpenAI function.arguments JSON strings to mappings before this template; preserve source bytes separately.",
            "Stock template does not supply assistant masks. Do not assume assistant_only_loss=True is usable unchanged.",
            "HAR64 must validate explicit target-only labels or a render-equivalent annotated template; observations/system/user/padding excluded.",
            "Pin clear_thinking and reasoning_effort to actual rollout settings. OMP xhigh is not a GLM effort setting.",
            "Final response stop/yield tokens need an explicit source-backed policy; template alone does not append terminal EOS.",
            "These fixture checks do not certify demonstrations, GPU backward, saved updates, serving or capability improvement.",
        ],
        "fixture_kind": "owned_protocol_fixture_not_training_data",
        "fixture_messages": MESSAGES,
        "rendered_example": rendered,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path(__file__).with_name("template-sources.json"))
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="Download only missing pinned metadata/tokenizer files; never weights")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("verification.json"))
    args = parser.parse_args()
    manifest = json.loads(args.sources.read_text())
    assets = acquire_assets(manifest, args.assets_dir, download=args.download)
    result = qualify(args.assets_dir)
    all_passed = all(check["passed"] for check in result["checks"])
    result = {
        "issue": "HAR-66", "evidence_kind": "cpu_metadata_tokenizer_protocol_fixture",
        "status": "checks_passed_with_explicit_training_gaps" if all_passed else "probe_failed",
        "created_at": datetime.now(UTC).isoformat(),
        "model_id": MODEL_ID, "revision": REVISION,
        "sources_sha256": sha256(args.sources.read_bytes()), "probe_sha256": sha256(Path(__file__).read_bytes()),
        "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                    "packages": {name: importlib.metadata.version(name) for name in ["transformers", "tokenizers", "jinja2", "pyyaml"]}},
        "model_weights_loaded": False, "gpu_used": False, "model_calls": 0,
        "assets": assets, **result,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": result["status"], "checks": len(result["checks"]),
                      "passed": sum(check["passed"] for check in result["checks"]),
                      "native_assistant_mask_tokens": result["protocol"]["native_assistant_mask"]["supervised_tokens"],
                      "receipt": str(args.output)}, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
