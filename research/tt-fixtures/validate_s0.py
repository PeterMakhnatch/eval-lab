from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from evallab.benchmark_program_contracts import compute_prefixed_sha256
from evallab.trainer_bundle import (
    TrainerBackendIdentityV1,
    TrainerBundleV1,
    TrainerRenderingContractV1,
    render_trl_plan,
    trainer_plan_digest,
)
from evallab.training_export import TrainingMessage, TrainingTool, _valid_tool_linkage

FIXTURE_ROOT = Path(__file__).resolve().parent
S0_ROOT = FIXTURE_ROOT / "s0-qwen3-0.6b"
FORBIDDEN_FIELDS = {
    "assistant_mask",
    "attention_mask",
    "completion_mask",
    "input_ids",
    "label",
    "labels",
    "log_probs",
    "logprobs",
    "loss_mask",
    "mask",
    "masks",
    "reward",
    "rewards",
    "token_id",
    "token_ids",
    "verifier_reward",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _message_payload(message: TrainingMessage) -> dict[str, Any]:
    return message.model_dump(
        mode="json",
        exclude={"sequence", "visibility"},
        exclude_none=True,
    )


def _project_fixture_records(messages: tuple[TrainingMessage, ...]) -> list[dict[str, Any]]:
    ordered = sorted(messages, key=lambda message: message.sequence)
    records: list[dict[str, Any]] = []
    for index, message in enumerate(ordered):
        if message.role != "assistant":
            continue
        records.append(
            {
                "target_sequence": message.sequence,
                "prompt": [_message_payload(prior) for prior in ordered[:index]],
                "response": _message_payload(message),
                "supervised_payload_fields": ["response"],
            }
        )
    return records


def _walk_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_walk_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_walk_keys(item) for item in value), set())
    return set()


def _validate_pure_trl_plan(spec: dict[str, Any]) -> dict[str, Any]:
    bundle_root = S0_ROOT / "trainer-bundle"
    bundle = TrainerBundleV1.model_validate_json(
        (bundle_root / "trainer-bundle.json").read_text(encoding="utf-8")
    )
    checkpoint = bundle.model_identity.checkpoint
    assert checkpoint is not None
    checkpoint_path = bundle_root / checkpoint.path
    checkpoint_digest = compute_prefixed_sha256(checkpoint_path.read_bytes())
    assert checkpoint_digest == spec["model_identity"]["checkpoint_digest"]
    assert checkpoint_digest == checkpoint.content_digest
    identity = spec["model_identity"]
    tokenizer_digest = compute_prefixed_sha256(
        (S0_ROOT / identity["tokenizer_revision"]).read_bytes()
    )
    template_digest = compute_prefixed_sha256(
        (S0_ROOT / identity["chat_template_revision"]).read_bytes()
    )
    assert tokenizer_digest == identity["tokenizer_digest"]
    assert template_digest == identity["chat_template_digest"]
    assert bundle.model_identity.tokenizer.digest == tokenizer_digest
    assert bundle.model_identity.chat_template.digest == template_digest
    assert bundle.model_identity.model.digest == checkpoint_digest

    backend_spec = spec["backend_identity"]
    backend = TrainerBackendIdentityV1(
        name=backend_spec["name"],
        version=backend_spec["version"],
        source_commit=backend_spec["source_commit"],
        image_digest=backend_spec["image_digest"],
    )
    trl_loaded_before = "trl" in sys.modules
    first = render_trl_plan(bundle, bundle_root, backend)
    second = render_trl_plan(bundle, bundle_root, backend)
    assert first == second
    assert trainer_plan_digest(first) == first.expected_result.trainer_plan_digest
    assert first.payload.sft_format == "prompt_completion"
    assert first.payload.prompt_field == "prompt"
    assert first.payload.completion_field == "response"
    assert first.payload.truncation == "error"
    assert first.payload.assistant_only_loss is False
    assert first.model_identity.enable_thinking is False
    assert ("trl" in sys.modules) == trl_loaded_before
    return {
        "deterministic": True,
        "plan_digest": trainer_plan_digest(first),
        "effective_config_digest": first.expected_result.effective_config_digest,
        "checkpoint_digest": first.expected_result.input_model_checkpoint_digest,
        "trl_imported_by_renderer": False,
        "assistant_only_loss_in_plan": first.payload.assistant_only_loss,
        "truncation": first.payload.truncation,
    }


def main() -> int:
    conversation = _load_json(S0_ROOT / "conversation.json")
    expected = _load_json(S0_ROOT / "expected-render.json")
    spec = _load_json(S0_ROOT / "fixture-spec.json")
    staging = _load_json(FIXTURE_ROOT / "s1-four-arm-render-staging.json")

    messages = tuple(TrainingMessage.model_validate(item) for item in conversation["messages"])
    tools = tuple(TrainingTool.model_validate(item) for item in conversation["tools"])
    assert tuple(message.sequence for message in messages) == tuple(range(len(messages)))
    assert _valid_tool_linkage(SimpleNamespace(messages=messages, tools=tools))

    projected = _project_fixture_records(messages)
    assert projected == expected["records"]
    assert projected == _project_fixture_records(messages)
    assert all(record["response"]["role"] == "assistant" for record in projected)
    assert all(record["supervised_payload_fields"] == ["response"] for record in projected)
    assert not (_walk_keys(projected) & FORBIDDEN_FIELDS)
    tool_payloads = [tool.model_dump(mode="json") for tool in tools]
    expected_training_payloads = [
        {
            "prompt": record["prompt"],
            "response": record["response"],
            "tools": tool_payloads,
        }
        for record in projected
    ]
    train_records = [
        json.loads(line)
        for line in (S0_ROOT / "trainer-bundle/train.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    train_payloads = [record["payload"] for record in train_records]
    assert sorted(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) for payload in train_payloads
    ) == sorted(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        for payload in expected_training_payloads
    )
    assert not (_walk_keys(train_payloads) & FORBIDDEN_FIELDS)

    tool_call = projected[0]["response"]["tool_calls"][0]
    tool_result = projected[1]["prompt"][-1]
    assert tool_call["id"] == tool_result["tool_call_id"] == "call_read_alpha"
    assert tool_call["function"]["name"] == tool_result["name"] == "read_record"
    assert (
        json.dumps(
            json.loads(tool_call["function"]["arguments"]), separators=(",", ":"), sort_keys=True
        )
        == tool_call["function"]["arguments"]
    )
    assert tool_result["content"] == '{"record_id":"alpha","value":"blue"}'
    assert projected[1]["response"]["content"] == "The authoritative value for alpha is blue."
    assert "[truncated]" not in json.dumps(projected).casefold()

    assistant_only_contract_rejected = False
    try:
        TrainerRenderingContractV1(
            representation="prompt_response_sft",
            sft_format="prompt_completion",
            prompt_field="prompt",
            completion_field="response",
            assistant_only_loss=True,
        )
    except ValidationError:
        assistant_only_contract_rejected = True
    assert assistant_only_contract_rejected

    assert staging["status"] == "blocked_before_bundle_materialization"
    assert [arm["arm"] for arm in staging["arms"]] == ["A", "B", "C", "D"]
    assert all(arm["bundle_manifest_status"] == "not_materialized" for arm in staging["arms"])
    assert all(arm["rendered_plan_status"] == "not_rendered" for arm in staging["arms"])
    assert staging["dependency_state"]["M3"]["status"] == "conditional_preregistration_g2_closed"
    assert [arm["recipe_disposition"] for arm in staging["arms"][2:]] == [
        "unavailable_degenerate_support",
        "unavailable_degenerate_support",
    ]

    plan = _validate_pure_trl_plan(spec)
    result = {
        "schema_version": "s0-validation-result/v1",
        "overall_status": "blocked_at_g3_assistant_mask_binding",
        "checks": {
            "deterministic_fixture_render": "pass",
            "assistant_response_target_boundary": "pass",
            "tool_call_round_trip": "pass",
            "forbidden_training_fields_absent": "pass",
            "string_level_semantic_preservation": "pass",
            "pure_trl_plan_render": "pass",
            "explicit_assistant_only_loss_binding": "blocked",
            "exact_qwen3_0.6b_token_mask_validation": "blocked",
            "s1_four_arm_bundle_render": "blocked",
        },
        "records": len(projected),
        "plan": plan,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
